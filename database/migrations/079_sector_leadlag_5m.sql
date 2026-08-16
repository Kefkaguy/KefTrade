-- Governed five-minute sector peer lead/lag research.
--
-- This is pre-strategy evidence only. It freezes a two-state x three-horizon
-- grid, records one discovery+validation spend per declaration, and permits at
-- most one candidate-only read of the untouched confirmation split.

CREATE TABLE IF NOT EXISTS intraday_sector_leadlag_declarations (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    cost_calibration_id BIGINT NOT NULL,
    specification JSONB NOT NULL,
    specification_hash TEXT NOT NULL UNIQUE,
    predictor_fingerprint JSONB NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_sector_leadlag_declaration_hash_check
        CHECK (length(specification_hash) = 64),
    CONSTRAINT intraday_sector_leadlag_declaration_immutable_check
        CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_sector_leadlag_runs (
    id BIGSERIAL PRIMARY KEY,
    declaration_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_sector_leadlag_declarations(id) ON DELETE RESTRICT,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    results JSONB NOT NULL,
    effective_trials INTEGER NOT NULL,
    candidate_cells JSONB NOT NULL DEFAULT '[]'::jsonb,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_sector_leadlag_run_trials_check
        CHECK (effective_trials > 0),
    CONSTRAINT intraday_sector_leadlag_run_immutable_check
        CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_sector_leadlag_confirmation_runs (
    id BIGSERIAL PRIMARY KEY,
    discovery_run_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_sector_leadlag_runs(id) ON DELETE RESTRICT,
    declaration_id BIGINT NOT NULL
        REFERENCES intraday_sector_leadlag_declarations(id) ON DELETE RESTRICT,
    results JSONB NOT NULL,
    passed BOOLEAN NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_sector_leadlag_confirmation_immutable_check
        CHECK (immutable = TRUE)
);

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'intraday_sector_leadlag_declarations',
        'intraday_sector_leadlag_runs',
        'intraday_sector_leadlag_confirmation_runs'
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
