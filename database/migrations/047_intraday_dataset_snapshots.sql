-- Phase 12.5 Step 1: schema only, no application-code wiring yet.
--
-- Extends the existing swing "reproducible research architecture"
-- (migration 028) to intraday research: an immutable snapshot of
-- intraday_features to sit alongside the already-generic research_dataset_candles
-- (which already works for 15m/30m candles unmodified), plus a dataset_kind
-- discriminator so a loader knows which companion feature table backs a
-- given manifest row. Also adds parent_campaign_id so a versioned re-run
-- (e.g. Phase 12.4's Campaign 50 relative to Campaign 47) can record its
-- baseline relationship as queryable data instead of only prose.
--
-- Purely additive: no existing table is dropped, no existing column type is
-- changed, no existing row in research_campaigns/research_campaign_jobs/
-- research_campaign_trades/research_dataset_manifests is touched. Campaign 47
-- and Campaign 50 rows are unaffected -- every new column here is nullable or
-- has a default that preserves today's implicit meaning ('swing').

CREATE TABLE IF NOT EXISTS research_dataset_intraday_features (
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    session_date DATE NOT NULL,
    minutes_from_open INTEGER,
    minutes_to_close INTEGER,
    session_vwap NUMERIC,
    distance_from_session_vwap NUMERIC,
    opening_range_high NUMERIC,
    opening_range_low NUMERIC,
    opening_range_position NUMERIC,
    gap_percent NUMERIC,
    session_relative_volume NUMERIC,
    PRIMARY KEY (dataset_id, symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS research_dataset_intraday_features_lookup_idx
    ON research_dataset_intraday_features(dataset_id, symbol, timeframe, timestamp);

DROP TRIGGER IF EXISTS research_dataset_intraday_features_immutable_trigger ON research_dataset_intraday_features;
CREATE TRIGGER research_dataset_intraday_features_immutable_trigger
    BEFORE UPDATE OR DELETE ON research_dataset_intraday_features
    FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation();

-- 'swing' is the correct default for every dataset manifest row created
-- before this migration -- they were all built from the swing candles+features
-- pipeline (record_dataset_snapshot), so backfilling them as 'swing' preserves
-- their true, unambiguous meaning rather than guessing.
ALTER TABLE research_dataset_manifests
    ADD COLUMN IF NOT EXISTS dataset_kind TEXT NOT NULL DEFAULT 'swing';

ALTER TABLE research_dataset_manifests DROP CONSTRAINT IF EXISTS research_dataset_manifests_dataset_kind_check;
ALTER TABLE research_dataset_manifests ADD CONSTRAINT research_dataset_manifests_dataset_kind_check
    CHECK (dataset_kind IN ('swing', 'intraday'));

CREATE INDEX IF NOT EXISTS research_dataset_manifests_kind_idx
    ON research_dataset_manifests(dataset_kind);

ALTER TABLE research_campaigns
    ADD COLUMN IF NOT EXISTS parent_campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL;
