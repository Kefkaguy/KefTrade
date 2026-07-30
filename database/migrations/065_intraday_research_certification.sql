-- Research-instrument certification and trial predeclaration.
--
-- Two problems are addressed here.  First, a run must be able to prove that
-- the measurement path recovers a factor that is really there and stays quiet
-- when one is not; the certification result is stored as evidence rather than
-- printed and discarded.  Second, a multiple-testing correction is only
-- honest if the trial count is the number of tests actually run, so tests are
-- declared before results exist and the declaration is immutable.

CREATE TABLE IF NOT EXISTS intraday_research_trial_declarations (
    id BIGSERIAL PRIMARY KEY,
    declaration_fingerprint TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    declared_factor_keys JSONB NOT NULL,
    declared_test_count INTEGER NOT NULL,
    hypothesis TEXT,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_declaration_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_declaration_count_check CHECK (declared_test_count >= 1),
    CONSTRAINT intraday_declaration_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_research_trial_declarations_lookup_idx
    ON intraday_research_trial_declarations(timeframe, created_at DESC);

-- Declarations are immutable, so consumption is recorded alongside rather
-- than by updating the declaration row.
CREATE TABLE IF NOT EXISTS intraday_research_trial_declaration_uses (
    id BIGSERIAL PRIMARY KEY,
    declaration_id BIGINT NOT NULL
        REFERENCES intraday_research_trial_declarations(id) ON DELETE RESTRICT,
    run_id BIGINT NOT NULL
        REFERENCES intraday_factor_diagnostic_runs(id) ON DELETE RESTRICT,
    tested_factor_keys JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_declaration_use_immutable_check CHECK (immutable = TRUE),
    UNIQUE(declaration_id, run_id)
);

CREATE TABLE IF NOT EXISTS intraday_research_certifications (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    timeframe TEXT NOT NULL,
    certified BOOLEAN NOT NULL,
    controls_passed BOOLEAN NOT NULL,
    leakage_passed BOOLEAN NOT NULL,
    calendar_passed BOOLEAN NOT NULL,
    controls JSONB NOT NULL DEFAULT '{}'::jsonb,
    leakage JSONB NOT NULL DEFAULT '{}'::jsonb,
    calendar JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_replication JSONB NOT NULL DEFAULT '{}'::jsonb,
    factor_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_certification_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_certification_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_research_certifications_lookup_idx
    ON intraday_research_certifications(dataset_id, timeframe, created_at DESC);

-- Link a factor run to the certification that was current when it ran, so a
-- stored result can never be read without the instrument evidence behind it.
ALTER TABLE intraday_factor_diagnostic_runs
    ADD COLUMN IF NOT EXISTS certification_id BIGINT
        REFERENCES intraday_research_certifications(id) ON DELETE RESTRICT;

ALTER TABLE intraday_factor_diagnostic_runs
    ADD COLUMN IF NOT EXISTS declaration_id BIGINT
        REFERENCES intraday_research_trial_declarations(id) ON DELETE RESTRICT;

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'intraday_research_trial_declarations',
        'intraday_research_trial_declaration_uses',
        'intraday_research_certifications'
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
