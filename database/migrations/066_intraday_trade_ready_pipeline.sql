-- Backend tables for the trade-ready 30m pipeline.
--
-- Everything an elite claim rests on becomes a referenceable, immutable row:
-- how the universe was constructed, whether the dataset could support the
-- experiment, what was hypothesised before any result existed, which factor
-- versions are permanently retired, and what the fills actually cost.

-- Ingestion progress is operational state, not evidence, so this is the one
-- table here that stays mutable: a resumed backfill must be able to update
-- the chunk it just finished.
CREATE TABLE IF NOT EXISTS intraday_candle_ingest_checkpoints (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    feed TEXT NOT NULL,
    chunk_start DATE NOT NULL,
    chunk_end DATE NOT NULL,
    status TEXT NOT NULL,
    bars_received INTEGER NOT NULL DEFAULT 0,
    bars_upserted INTEGER NOT NULL DEFAULT 0,
    invalid_bars INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    ingest_version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe, feed, chunk_start),
    CONSTRAINT intraday_ingest_checkpoint_status_check
        CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS intraday_candle_ingest_checkpoints_status_idx
    ON intraday_candle_ingest_checkpoints(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS research_universe_definitions (
    id BIGSERIAL PRIMARY KEY,
    universe_key TEXT NOT NULL,
    rule_hash TEXT NOT NULL,
    universe_version TEXT NOT NULL,
    construction_rule JSONB NOT NULL,
    candidate_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    window_start DATE NOT NULL,
    window_end DATE NOT NULL,
    survivorship_audit JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_universe_definition_immutable_check CHECK (immutable = TRUE),
    UNIQUE (universe_key, rule_hash)
);

CREATE TABLE IF NOT EXISTS intraday_dataset_quality_reports (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    timeframe TEXT NOT NULL,
    quality_passed BOOLEAN NOT NULL,
    power_passed BOOLEAN NOT NULL,
    ready_for_discovery BOOLEAN NOT NULL,
    report JSONB NOT NULL DEFAULT '{}'::jsonb,
    quality_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_dataset_quality_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_dataset_quality_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_dataset_quality_reports_lookup_idx
    ON intraday_dataset_quality_reports(dataset_id, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS intraday_research_hypotheses (
    id BIGSERIAL PRIMARY KEY,
    hypothesis_key TEXT NOT NULL,
    hypothesis_hash TEXT NOT NULL UNIQUE,
    experiment_key TEXT NOT NULL,
    factor_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    horizon_bars INTEGER NOT NULL DEFAULT 1,
    expected_direction TEXT NOT NULL,
    required_event_count INTEGER NOT NULL,
    hypothesis JSONB NOT NULL,
    hypothesis_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_hypothesis_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_hypothesis_direction_check
        CHECK (expected_direction IN ('long', 'short', 'both')),
    CONSTRAINT intraday_hypothesis_version_check CHECK (version >= 1),
    CONSTRAINT intraday_hypothesis_events_check CHECK (required_event_count >= 1),
    CONSTRAINT intraday_hypothesis_immutable_check CHECK (immutable = TRUE),
    UNIQUE (hypothesis_key, version)
);

CREATE INDEX IF NOT EXISTS intraday_research_hypotheses_experiment_idx
    ON intraday_research_hypotheses(experiment_key, timeframe, created_at);

-- Retirement is permanent: a version that failed locked confirmation, or that
-- produced an interpretable null, does not get another attempt.
CREATE TABLE IF NOT EXISTS intraday_retired_factor_versions (
    id BIGSERIAL PRIMARY KEY,
    factor_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    hypothesis_hash TEXT,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_retired_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_retired_immutable_check CHECK (immutable = TRUE),
    UNIQUE (factor_key, spec_hash)
);

CREATE TABLE IF NOT EXISTS intraday_strategy_families (
    id BIGSERIAL PRIMARY KEY,
    factor_key TEXT NOT NULL,
    recipe_hash TEXT NOT NULL UNIQUE,
    confirmation_run_id BIGINT NOT NULL
        REFERENCES intraday_factor_diagnostic_runs(id) ON DELETE RESTRICT,
    cost_calibration_id BIGINT,
    recipe JSONB NOT NULL,
    gates_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_strategy_family_immutable_check CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_fill_calibrations (
    id BIGSERIAL PRIMARY KEY,
    family_id BIGINT REFERENCES intraday_strategy_families(id) ON DELETE RESTRICT,
    factor_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    matched_fills INTEGER NOT NULL,
    passed BOOLEAN NOT NULL,
    report JSONB NOT NULL DEFAULT '{}'::jsonb,
    gates_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_fill_calibration_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_fill_calibration_immutable_check CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_elite_qualifications (
    id BIGSERIAL PRIMARY KEY,
    factor_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    qualified BOOLEAN NOT NULL,
    verdict JSONB NOT NULL,
    gates_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_elite_qualification_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_elite_qualification_immutable_check CHECK (immutable = TRUE)
);

-- A run records which dataset quality report and hypothesis it was authorised by.
ALTER TABLE intraday_factor_diagnostic_runs
    ADD COLUMN IF NOT EXISTS quality_report_id BIGINT
        REFERENCES intraday_dataset_quality_reports(id) ON DELETE RESTRICT;

ALTER TABLE intraday_factor_diagnostic_runs
    ADD COLUMN IF NOT EXISTS experiment_key TEXT;

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'research_universe_definitions',
        'intraday_dataset_quality_reports',
        'intraday_research_hypotheses',
        'intraday_retired_factor_versions',
        'intraday_strategy_families',
        'intraday_fill_calibrations',
        'intraday_elite_qualifications'
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
