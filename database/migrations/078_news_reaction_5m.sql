-- Governed five-minute news-reaction research.
--
-- This is pre-strategy evidence only.  It freezes a small event/state grid,
-- records one discovery+validation run per declaration, and permits at most
-- one later confirmation read.  It cannot authorize broker action.

CREATE TABLE IF NOT EXISTS intraday_news_reaction_declarations (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    cost_calibration_id BIGINT NOT NULL,
    specification JSONB NOT NULL,
    specification_hash TEXT NOT NULL UNIQUE,
    news_fingerprint JSONB NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_news_reaction_declaration_hash_check
        CHECK (length(specification_hash) = 64),
    CONSTRAINT intraday_news_reaction_declaration_immutable_check
        CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_news_reaction_runs (
    id BIGSERIAL PRIMARY KEY,
    declaration_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_news_reaction_declarations(id) ON DELETE RESTRICT,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    results JSONB NOT NULL,
    effective_trials INTEGER NOT NULL,
    candidate_cells JSONB NOT NULL DEFAULT '[]'::jsonb,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_news_reaction_run_trials_check
        CHECK (effective_trials > 0),
    CONSTRAINT intraday_news_reaction_run_immutable_check
        CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_news_reaction_confirmation_runs (
    id BIGSERIAL PRIMARY KEY,
    discovery_run_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_news_reaction_runs(id) ON DELETE RESTRICT,
    declaration_id BIGINT NOT NULL
        REFERENCES intraday_news_reaction_declarations(id) ON DELETE RESTRICT,
    results JSONB NOT NULL,
    passed BOOLEAN NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_news_reaction_confirmation_immutable_check
        CHECK (immutable = TRUE)
);

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'intraday_news_reaction_declarations',
        'intraday_news_reaction_runs',
        'intraday_news_reaction_confirmation_runs'
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
