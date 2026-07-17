CREATE TABLE IF NOT EXISTS research_dataset_manifests (
    id BIGSERIAL PRIMARY KEY,
    dataset_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL DEFAULT 1,
    assets JSONB NOT NULL,
    timeframes JSONB NOT NULL,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    candle_counts JSONB NOT NULL,
    candle_hashes JSONB NOT NULL,
    source_providers JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_hash TEXT NOT NULL,
    integrity JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_dataset_manifests_mode_check CHECK (mode IN ('reproducibility', 'rolling')),
    CONSTRAINT research_dataset_manifests_assets_check CHECK (jsonb_typeof(assets) = 'array'),
    CONSTRAINT research_dataset_manifests_timeframes_check CHECK (jsonb_typeof(timeframes) = 'array'),
    CONSTRAINT research_dataset_manifests_immutable_check CHECK (immutable = TRUE),
    CONSTRAINT research_dataset_manifests_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_dataset_manifests_created_idx
    ON research_dataset_manifests(created_at DESC);

CREATE TABLE IF NOT EXISTS research_dataset_candles (
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    PRIMARY KEY(dataset_id, symbol, timeframe, timestamp, source),
    CONSTRAINT research_dataset_candles_open_positive_check CHECK (open > 0),
    CONSTRAINT research_dataset_candles_high_positive_check CHECK (high > 0),
    CONSTRAINT research_dataset_candles_low_positive_check CHECK (low > 0),
    CONSTRAINT research_dataset_candles_close_positive_check CHECK (close > 0),
    CONSTRAINT research_dataset_candles_volume_nonnegative_check CHECK (volume >= 0)
);

CREATE INDEX IF NOT EXISTS research_dataset_candles_lookup_idx
    ON research_dataset_candles(dataset_id, symbol, timeframe, timestamp);

CREATE TABLE IF NOT EXISTS asset_profile_versions (
    id BIGSERIAL PRIMARY KEY,
    profile_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    evidence_window JSONB NOT NULL,
    metrics JSONB NOT NULL,
    behavior_labels JSONB NOT NULL,
    regime_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlations JSONB NOT NULL DEFAULT '{}'::jsonb,
    limitations JSONB NOT NULL DEFAULT '[]'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT asset_profile_versions_unique UNIQUE(profile_key, version),
    CONSTRAINT asset_profile_versions_dataset_unique UNIQUE(dataset_id, symbol, timeframe),
    CONSTRAINT asset_profile_versions_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS asset_profile_versions_symbol_idx
    ON asset_profile_versions(symbol, timeframe, version DESC);

CREATE TABLE IF NOT EXISTS asset_cluster_versions (
    id BIGSERIAL PRIMARY KEY,
    cluster_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    centroid JSONB NOT NULL,
    member_count INTEGER NOT NULL,
    quality_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    algorithm_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT asset_cluster_versions_unique UNIQUE(cluster_key, version),
    CONSTRAINT asset_cluster_versions_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS asset_cluster_members (
    cluster_id BIGINT NOT NULL REFERENCES asset_cluster_versions(id) ON DELETE CASCADE,
    asset_profile_id BIGINT NOT NULL REFERENCES asset_profile_versions(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    similarity_score NUMERIC NOT NULL,
    distance_to_centroid NUMERIC NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(cluster_id, asset_profile_id)
);

CREATE INDEX IF NOT EXISTS asset_cluster_members_symbol_idx
    ON asset_cluster_members(symbol, timeframe, cluster_id);

CREATE TABLE IF NOT EXISTS research_hypothesis_versions (
    id BIGSERIAL PRIMARY KEY,
    hypothesis_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_hypothesis_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
    scope_type TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    strategy_family TEXT NOT NULL,
    title TEXT NOT NULL,
    observation TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    expected_behavior TEXT NOT NULL,
    relevant_regimes JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence_score NUMERIC NOT NULL,
    evidence_window JSONB NOT NULL,
    creation_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    supporting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    contradictory_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    test_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_hypothesis_versions_unique UNIQUE(hypothesis_key, version),
    CONSTRAINT research_hypothesis_versions_scope_check CHECK (scope_type IN ('asset', 'cluster', 'universal')),
    CONSTRAINT research_hypothesis_versions_status_check CHECK (status IN ('proposed', 'testing', 'supported', 'weak', 'rejected', 'retired')),
    CONSTRAINT research_hypothesis_versions_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_hypothesis_versions_status_idx
    ON research_hypothesis_versions(status, confidence_score DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS research_validation_policy_versions (
    id BIGSERIAL PRIMARY KEY,
    policy_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    thresholds JSONB NOT NULL,
    approval JSONB NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_validation_policy_versions_unique UNIQUE(policy_key, version),
    CONSTRAINT research_validation_policy_versions_immutable_check CHECK (immutable = TRUE),
    CONSTRAINT research_validation_policy_versions_simulation_only_check CHECK (simulation_only = TRUE)
);

ALTER TABLE research_campaigns
    ADD COLUMN IF NOT EXISTS dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS dataset_mode TEXT,
    ADD COLUMN IF NOT EXISTS code_commit TEXT,
    ADD COLUMN IF NOT EXISTS generator_version TEXT,
    ADD COLUMN IF NOT EXISTS validation_policy_id BIGINT REFERENCES research_validation_policy_versions(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS threshold_version TEXT,
    ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS cluster_id BIGINT REFERENCES asset_cluster_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS experiment_generation INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS immutable_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ;

ALTER TABLE research_campaigns DROP CONSTRAINT IF EXISTS research_campaigns_dataset_mode_check;
ALTER TABLE research_campaigns ADD CONSTRAINT research_campaigns_dataset_mode_check
    CHECK (dataset_mode IS NULL OR dataset_mode IN ('reproducibility', 'rolling'));

ALTER TABLE research_campaign_jobs
    ADD COLUMN IF NOT EXISTS dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS parent_candidate_id TEXT,
    ADD COLUMN IF NOT EXISTS generation_channel TEXT,
    ADD COLUMN IF NOT EXISTS rejection_diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE research_campaign_jobs DROP CONSTRAINT IF EXISTS research_campaign_jobs_generation_channel_check;
ALTER TABLE research_campaign_jobs ADD CONSTRAINT research_campaign_jobs_generation_channel_check
    CHECK (generation_channel IS NULL OR generation_channel IN ('exploitation', 'nearby', 'exploration'));

ALTER TABLE elite_research_candidates
    ADD COLUMN IF NOT EXISTS candidate_level TEXT NOT NULL DEFAULT 'cluster_elite',
    ADD COLUMN IF NOT EXISTS scope_type TEXT,
    ADD COLUMN IF NOT EXISTS scope_ref TEXT,
    ADD COLUMN IF NOT EXISTS dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS parent_candidate_id TEXT;

CREATE TABLE IF NOT EXISTS research_candidate_stage_evidence (
    id BIGSERIAL PRIMARY KEY,
    evidence_key TEXT NOT NULL UNIQUE,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    candidate_level TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
    parent_candidate_id TEXT,
    gate_results JSONB NOT NULL,
    metrics JSONB NOT NULL,
    evidence_refs JSONB NOT NULL,
    promoted BOOLEAN NOT NULL DEFAULT FALSE,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_candidate_stage_level_check CHECK (candidate_level IN (
        'generated', 'research_candidate', 'asset_specialist', 'cluster_candidate', 'cluster_elite', 'universal_elite'
    )),
    CONSTRAINT research_candidate_stage_scope_check CHECK (scope_type IN ('asset', 'cluster', 'universal')),
    CONSTRAINT research_candidate_stage_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_candidate_stage_campaign_idx
    ON research_candidate_stage_evidence(campaign_id, candidate_level, promoted);

CREATE TABLE IF NOT EXISTS research_campaign_archives (
    id BIGSERIAL PRIMARY KEY,
    archive_key TEXT NOT NULL UNIQUE,
    campaign_id BIGINT,
    original_campaign_id BIGINT NOT NULL,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    manifest JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    storage_locations JSONB NOT NULL DEFAULT '[]'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_archives_immutable_check CHECK (immutable = TRUE),
    CONSTRAINT research_campaign_archives_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_archives_campaign_idx
    ON research_campaign_archives(original_campaign_id, created_at DESC);

CREATE TABLE IF NOT EXISTS autonomous_research_cycles (
    id BIGSERIAL PRIMARY KEY,
    cycle_key TEXT NOT NULL UNIQUE,
    universe_key TEXT NOT NULL,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE SET NULL,
    cluster_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    hypothesis_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    approval_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    plan JSONB NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    safety_controls JSONB NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT autonomous_research_cycles_approval_check CHECK (approval_mode IN ('manual', 'auto_queue')),
    CONSTRAINT autonomous_research_cycles_status_check CHECK (status IN ('planned', 'queued', 'completed', 'failed', 'paused')),
    CONSTRAINT autonomous_research_cycles_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE OR REPLACE FUNCTION prevent_immutable_research_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'immutable research evidence cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_dataset_manifests_immutable_trigger ON research_dataset_manifests;
CREATE TRIGGER research_dataset_manifests_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_dataset_manifests
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS research_dataset_candles_immutable_trigger ON research_dataset_candles;
CREATE TRIGGER research_dataset_candles_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_dataset_candles
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS asset_profile_versions_immutable_trigger ON asset_profile_versions;
CREATE TRIGGER asset_profile_versions_immutable_trigger
    BEFORE UPDATE OR DELETE ON asset_profile_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS asset_cluster_versions_immutable_trigger ON asset_cluster_versions;
CREATE TRIGGER asset_cluster_versions_immutable_trigger
    BEFORE UPDATE OR DELETE ON asset_cluster_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS asset_cluster_members_immutable_trigger ON asset_cluster_members;
CREATE TRIGGER asset_cluster_members_immutable_trigger
    BEFORE UPDATE OR DELETE ON asset_cluster_members
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS research_hypothesis_versions_immutable_trigger ON research_hypothesis_versions;
CREATE TRIGGER research_hypothesis_versions_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_hypothesis_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS research_candidate_stage_evidence_immutable_trigger ON research_candidate_stage_evidence;
CREATE TRIGGER research_candidate_stage_evidence_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_candidate_stage_evidence
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS research_validation_policy_immutable_trigger ON research_validation_policy_versions;
CREATE TRIGGER research_validation_policy_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_validation_policy_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

DROP TRIGGER IF EXISTS research_campaign_archives_immutable_trigger ON research_campaign_archives;
CREATE TRIGGER research_campaign_archives_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_campaign_archives
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

INSERT INTO research_validation_policy_versions(
    policy_key, version, name, thresholds, approval, calculation_version, immutable, simulation_only
)
VALUES (
    'strong_research_gates',
    1,
    'Strong research validation gates',
    '{
      "single_market": {
        "minimum_profit_factor": 1.2,
        "minimum_expectancy_per_trade": 0,
        "maximum_drawdown": 0.12,
        "minimum_trades": 30,
        "walk_forward_required": true,
        "paper_readiness_required": true
      },
      "cross_market": {
        "minimum_profit_factor": 1.2,
        "minimum_expectancy": 0,
        "maximum_drawdown": 0.12,
        "minimum_trades": 60,
        "minimum_stability": 0.6,
        "minimum_assets_passed": 2,
        "minimum_timeframes_passed": 1
      }
    }'::jsonb,
    '{"threshold_changes_require_explicit_version": true, "automatic_weakening_forbidden": true}'::jsonb,
    'research_validation_policy_v1',
    TRUE,
    TRUE
)
ON CONFLICT(policy_key, version) DO NOTHING;
