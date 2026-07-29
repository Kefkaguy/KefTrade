-- Institutional controls for 15m/30m research.
--
-- Every tried family/variant/horizon is append-only evidence.  Market data
-- that cannot be reconstructed from candles (auction imbalances, point-in-
-- time universe membership, corporate actions) receives explicit backend
-- storage rather than an inferred placeholder.

CREATE TABLE IF NOT EXISTS intraday_research_trials (
    id BIGSERIAL PRIMARY KEY,
    trial_fingerprint TEXT NOT NULL,
    trial_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    architecture TEXT NOT NULL,
    family_name TEXT,
    candidate_id TEXT,
    timeframe TEXT NOT NULL,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    horizon_bars INTEGER,
    effective_trials INTEGER NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    split_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_model JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_research_trials_type_check
        CHECK (trial_type IN ('signal', 'factor')),
    CONSTRAINT intraday_research_trials_phase_check
        CHECK (phase IN ('discovery', 'validation', 'confirmation')),
    CONSTRAINT intraday_research_trials_timeframe_check
        CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_research_trials_effective_check
        CHECK (effective_trials >= 1),
    CONSTRAINT intraday_research_trials_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_research_trials_lookup_idx
    ON intraday_research_trials(dataset_id, timeframe, architecture, created_at);

CREATE INDEX IF NOT EXISTS intraday_research_trials_phase_idx
    ON intraday_research_trials(phase, created_at);

CREATE TABLE IF NOT EXISTS intraday_auction_imbalances (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    auction_type TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    session_date DATE NOT NULL,
    reference_price NUMERIC,
    midpoint_at_message NUMERIC,
    near_clearing_price NUMERIC,
    far_clearing_price NUMERIC,
    auction_price NUMERIC,
    paired_quantity NUMERIC,
    imbalance_quantity NUMERIC NOT NULL,
    imbalance_side TEXT NOT NULL,
    provider TEXT NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_auction_type_check CHECK (auction_type IN ('open', 'close')),
    CONSTRAINT intraday_auction_side_check CHECK (imbalance_side IN ('buy', 'sell', 'none')),
    CONSTRAINT intraday_auction_quantity_check CHECK (
        imbalance_quantity >= 0 AND (paired_quantity IS NULL OR paired_quantity >= 0)
    ),
    CONSTRAINT intraday_auction_immutable_check CHECK (immutable = TRUE),
    UNIQUE(symbol, exchange, auction_type, timestamp, provider)
);

CREATE INDEX IF NOT EXISTS intraday_auction_imbalances_lookup_idx
    ON intraday_auction_imbalances(symbol, session_date, auction_type, timestamp);

CREATE TABLE IF NOT EXISTS research_point_in_time_universe_membership (
    id BIGSERIAL PRIMARY KEY,
    universe_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    source TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_universe_dates_check
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT research_universe_immutable_check CHECK (immutable = TRUE),
    UNIQUE(universe_key, symbol, effective_from, source)
);

CREATE INDEX IF NOT EXISTS research_universe_membership_lookup_idx
    ON research_point_in_time_universe_membership(universe_key, effective_from, effective_to);

CREATE TABLE IF NOT EXISTS research_corporate_actions (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date DATE NOT NULL,
    adjustment_factor NUMERIC,
    cash_amount NUMERIC,
    source TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_corporate_action_type_check
        CHECK (action_type IN ('split', 'dividend', 'symbol_change', 'delisting')),
    CONSTRAINT research_corporate_action_immutable_check CHECK (immutable = TRUE),
    UNIQUE(symbol, action_type, effective_date, source)
);

CREATE OR REPLACE FUNCTION prevent_intraday_research_evidence_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'immutable intraday research evidence cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_name TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'intraday_research_trials',
        'intraday_auction_imbalances',
        'research_point_in_time_universe_membership',
        'research_corporate_actions'
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
