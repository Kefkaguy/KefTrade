-- Phase 12.5 Step 1: schema only, no deployment/promotion logic, no
-- application-code wiring yet.
--
-- research_specialist_threads tracks a narrow-but-real finding (e.g. AMD 30m
-- long Session Momentum from Phase 12.4) as a long-lived research object,
-- separate from the campaign-scoped elite-candidate ladder. Its `status`
-- column is expected to change over the thread's life (active_research ->
-- confirmed_specialist / invalidated -> retired) as new investigations land,
-- so the row itself is not blanket-immutable like the other tables in this
-- migration set -- only `frozen_parameters` (the exact rule definition being
-- investigated) is protected from mutation, via a dedicated trigger that
-- compares OLD/NEW rather than rejecting every UPDATE. Deleting a thread is
-- still rejected outright: a specialist thread, once created, is never
-- silently removed.
--
-- research_specialist_investigations is the append-only lab notebook: one
-- immutable row per investigation (holdout performance, forward validation,
-- parameter robustness, cost robustness, cross-year stability, similarity to
-- pre-declared comparison securities -- see the architecture proposal,
-- section 5). It reuses the existing blanket immutability trigger.
--
-- No column here grants any deployment, promotion, or live/paper-trading
-- capability -- this migration only records research evidence.

CREATE TABLE IF NOT EXISTS research_specialist_threads (
    id BIGSERIAL PRIMARY KEY,
    thread_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    origin_campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE RESTRICT,
    origin_candidate_id TEXT NOT NULL,
    frozen_parameters JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'active_research',
    scope_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    scope_timeframe TEXT NOT NULL,
    scope_direction TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_specialist_threads_status_check
        CHECK (status IN ('active_research', 'confirmed_specialist', 'invalidated', 'retired')),
    CONSTRAINT research_specialist_threads_scope_symbols_check CHECK (jsonb_typeof(scope_symbols) = 'array'),
    CONSTRAINT research_specialist_threads_scope_direction_check CHECK (scope_direction IN ('long', 'short')),
    CONSTRAINT research_specialist_threads_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_specialist_threads_status_idx
    ON research_specialist_threads(status, created_at DESC);

CREATE OR REPLACE FUNCTION prevent_frozen_parameters_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'research_specialist_threads rows cannot be deleted';
    END IF;
    IF NEW.frozen_parameters IS DISTINCT FROM OLD.frozen_parameters THEN
        RAISE EXCEPTION 'research_specialist_threads.frozen_parameters is immutable once set';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_specialist_threads_frozen_parameters_trigger ON research_specialist_threads;
CREATE TRIGGER research_specialist_threads_frozen_parameters_trigger
    BEFORE UPDATE ON research_specialist_threads
    FOR EACH ROW EXECUTE FUNCTION prevent_frozen_parameters_mutation();

DROP TRIGGER IF EXISTS research_specialist_threads_no_delete_trigger ON research_specialist_threads;
CREATE TRIGGER research_specialist_threads_no_delete_trigger
    BEFORE DELETE ON research_specialist_threads
    FOR EACH ROW EXECUTE FUNCTION prevent_frozen_parameters_mutation();

CREATE TABLE IF NOT EXISTS research_specialist_investigations (
    id BIGSERIAL PRIMARY KEY,
    thread_id BIGINT NOT NULL REFERENCES research_specialist_threads(id) ON DELETE CASCADE,
    investigation_type TEXT NOT NULL,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    findings JSONB NOT NULL DEFAULT '{}'::jsonb,
    conclusion TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_specialist_investigations_type_check CHECK (investigation_type IN (
        'unseen_holdout_performance',
        'forward_validation',
        'parameter_robustness',
        'cost_robustness',
        'stability_across_years',
        'similarity_to_declared_securities'
    )),
    CONSTRAINT research_specialist_investigations_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_specialist_investigations_thread_idx
    ON research_specialist_investigations(thread_id, created_at DESC);

DROP TRIGGER IF EXISTS research_specialist_investigations_immutable_trigger ON research_specialist_investigations;
CREATE TRIGGER research_specialist_investigations_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_specialist_investigations
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

-- Hypothesis-registry additions from the architecture proposal, section 6.
-- All nullable: existing research_hypothesis_versions rows (every swing
-- hypothesis created before this migration) simply have no value for these
-- three fields rather than a fabricated one. success_criteria is intended to
-- be populated once, before a campaign for that hypothesis launches --
-- enforcement of that ordering is application-code, not a schema constraint,
-- since a database CHECK cannot see "was this written before a campaign
-- existed."
ALTER TABLE research_hypothesis_versions
    ADD COLUMN IF NOT EXISTS required_conditions TEXT,
    ADD COLUMN IF NOT EXISTS invalidation_conditions TEXT,
    ADD COLUMN IF NOT EXISTS success_criteria JSONB;
