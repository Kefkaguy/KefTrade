-- Event-conditioned alpha discovery.
--
-- This layer deliberately sits before strategy construction.  It stores a
-- predeclared event/context specification, development-only event evidence, a
-- frozen score/veto model, and at most one later confirmation result.  None of
-- these tables authorizes a campaign or broker action.

CREATE TABLE IF NOT EXISTS intraday_event_study_declarations (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    timeframe TEXT NOT NULL,
    branches JSONB NOT NULL,
    symbols JSONB NOT NULL,
    horizons_minutes JSONB NOT NULL,
    feature_catalog JSONB NOT NULL,
    split_boundaries JSONB NOT NULL,
    cost_model JSONB NOT NULL,
    specification JSONB NOT NULL,
    specification_hash TEXT NOT NULL UNIQUE,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_event_declaration_timeframe_check
        CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_event_declaration_hash_check
        CHECK (length(specification_hash) = 64),
    CONSTRAINT intraday_event_declaration_immutable_check CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_event_study_runs (
    id BIGSERIAL PRIMARY KEY,
    declaration_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_event_study_declarations(id) ON DELETE RESTRICT,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    results JSONB NOT NULL,
    frozen_model JSONB NOT NULL,
    effective_trials INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_event_study_trials_check CHECK (effective_trials > 0),
    CONSTRAINT intraday_event_study_count_check CHECK (event_count >= 0),
    CONSTRAINT intraday_event_study_immutable_check CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_event_study_events (
    run_id BIGINT NOT NULL
        REFERENCES intraday_event_study_runs(id) ON DELETE RESTRICT,
    event_key TEXT NOT NULL,
    branch TEXT NOT NULL,
    stage TEXT NOT NULL,
    symbol TEXT NOT NULL,
    session_date DATE NOT NULL,
    decision_timestamp TIMESTAMPTZ NOT NULL,
    direction TEXT NOT NULL,
    phase TEXT NOT NULL,
    features JSONB NOT NULL,
    outcomes JSONB NOT NULL,
    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_bps NUMERIC NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (run_id, event_key, symbol, decision_timestamp),
    CONSTRAINT intraday_event_direction_check CHECK (direction IN ('long', 'short')),
    CONSTRAINT intraday_event_phase_check CHECK (phase IN ('discovery', 'validation')),
    CONSTRAINT intraday_event_cost_check CHECK (cost_bps >= 0),
    CONSTRAINT intraday_event_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_event_study_events_lookup_idx
    ON intraday_event_study_events(run_id, event_key, phase, session_date);

CREATE TABLE IF NOT EXISTS intraday_event_confirmation_runs (
    id BIGSERIAL PRIMARY KEY,
    discovery_run_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_event_study_runs(id) ON DELETE RESTRICT,
    declaration_id BIGINT NOT NULL
        REFERENCES intraday_event_study_declarations(id) ON DELETE RESTRICT,
    model_hash TEXT NOT NULL UNIQUE,
    results JSONB NOT NULL,
    passed BOOLEAN NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_event_confirmation_hash_check CHECK (length(model_hash) = 64),
    CONSTRAINT intraday_event_confirmation_immutable_check CHECK (immutable = TRUE)
);

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'intraday_event_study_declarations',
        'intraday_event_study_runs',
        'intraday_event_study_events',
        'intraday_event_confirmation_runs'
    ]
    LOOP
        trigger_name := target || '_immutable_trigger';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, target);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I ' ||
            'FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation()',
            trigger_name,
            target
        );
    END LOOP;
END;
$$;
