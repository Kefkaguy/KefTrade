-- Backend-only 30m research-to-elite funnel.

ALTER TABLE research_dataset_intraday_features
    ADD COLUMN IF NOT EXISTS microstructure_provider TEXT,
    ADD COLUMN IF NOT EXISTS microstructure_feed TEXT,
    ADD COLUMN IF NOT EXISTS quote_count INTEGER,
    ADD COLUMN IF NOT EXISTS median_spread_bps NUMERIC,
    ADD COLUMN IF NOT EXISTS p90_spread_bps NUMERIC,
    ADD COLUMN IF NOT EXISTS mean_depth NUMERIC,
    ADD COLUMN IF NOT EXISTS order_flow_imbalance NUMERIC,
    ADD COLUMN IF NOT EXISTS normalized_order_flow_imbalance NUMERIC;

CREATE TABLE IF NOT EXISTS intraday_quote_ingestion_checkpoints (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    symbol TEXT NOT NULL,
    session_date DATE NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    quote_rows INTEGER NOT NULL DEFAULT 0,
    microstructure_rows INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_quote_checkpoint_status_check
        CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT intraday_quote_checkpoint_counts_check
        CHECK (attempts >= 0 AND quote_rows >= 0 AND microstructure_rows >= 0),
    UNIQUE(provider, feed, symbol, session_date)
);

CREATE INDEX IF NOT EXISTS intraday_quote_checkpoint_progress_idx
    ON intraday_quote_ingestion_checkpoints(feed, status, session_date, symbol);

CREATE TABLE IF NOT EXISTS intraday_executable_candidates (
    id BIGSERIAL PRIMARY KEY,
    source_factor_run_id BIGINT NOT NULL
        REFERENCES intraday_factor_diagnostic_runs(id) ON DELETE RESTRICT,
    factor_key TEXT NOT NULL,
    architecture TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    discovery_dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    cost_calibration_id BIGINT NOT NULL
        REFERENCES intraday_execution_cost_calibrations(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL,
    candidate JSONB NOT NULL,
    frozen_spec_hash TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_executable_candidate_timeframe_check CHECK (timeframe = '30m'),
    CONSTRAINT intraday_executable_candidate_hash_check CHECK (length(frozen_spec_hash) = 64),
    CONSTRAINT intraday_executable_candidate_immutable_check CHECK (immutable = TRUE),
    UNIQUE(source_factor_run_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS intraday_executable_candidate_factor_idx
    ON intraday_executable_candidates(factor_key, architecture, created_at);

CREATE TABLE IF NOT EXISTS intraday_executable_runs (
    id BIGSERIAL PRIMARY KEY,
    executable_candidate_id BIGINT NOT NULL
        REFERENCES intraday_executable_candidates(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    source_last_session_date DATE,
    signal_confirmation_passed BOOLEAN NOT NULL DEFAULT FALSE,
    simulation_passed BOOLEAN NOT NULL DEFAULT FALSE,
    result JSONB NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_executable_run_phase_check
        CHECK (phase IN ('development_simulation', 'locked_confirmation')),
    CONSTRAINT intraday_executable_run_immutable_check CHECK (immutable = TRUE),
    UNIQUE(executable_candidate_id, phase, dataset_id)
);

CREATE INDEX IF NOT EXISTS intraday_executable_run_lookup_idx
    ON intraday_executable_runs(phase, dataset_id, created_at);

CREATE TABLE IF NOT EXISTS intraday_family_activations (
    id BIGSERIAL PRIMARY KEY,
    architecture TEXT NOT NULL,
    factor_key TEXT NOT NULL,
    executable_candidate_id BIGINT NOT NULL
        REFERENCES intraday_executable_candidates(id) ON DELETE RESTRICT,
    confirmation_run_id BIGINT NOT NULL
        REFERENCES intraday_executable_runs(id) ON DELETE RESTRICT,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE RESTRICT,
    activation_state TEXT NOT NULL,
    activation_evidence JSONB NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_family_activation_state_check
        CHECK (activation_state IN ('campaign_eligible', 'elite_eligible')),
    CONSTRAINT intraday_family_activation_immutable_check CHECK (immutable = TRUE),
    UNIQUE(executable_candidate_id, confirmation_run_id, activation_state)
);

CREATE INDEX IF NOT EXISTS intraday_family_activation_lookup_idx
    ON intraday_family_activations(architecture, activation_state, created_at);

DO $$
DECLARE
    table_name TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'intraday_executable_candidates',
        'intraday_executable_runs',
        'intraday_family_activations'
    ]
    LOOP
        trigger_name := table_name || '_immutable_trigger';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I ' ||
            'FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation()',
            trigger_name,
            table_name
        );
    END LOOP;
END;
$$;
