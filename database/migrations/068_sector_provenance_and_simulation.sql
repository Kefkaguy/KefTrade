-- Sector provenance, and persisted execution simulations.
--
-- The sector map was thirteen rows written by hand, so a 237-symbol universe
-- reported eleven symbols with a sector and one usable peer group.  That is a
-- metadata hole, not a market fact, and the sector-relative family cannot be
-- measured until it is filled.
--
-- Sector classification is not available point-in-time from any source this
-- system has.  The backfill therefore applies a current classification to the
-- whole history, which is acceptable for a peer-group control and would not be
-- for a traded signal.  Rather than leave that assumption in a commit message,
-- it is stamped on every row.

CREATE TABLE IF NOT EXISTS symbol_sector_provenance (
    symbol TEXT PRIMARY KEY,
    sector TEXT NOT NULL,
    industry TEXT,
    source TEXT NOT NULL,
    -- Names the look-ahead this classification carries.
    provenance TEXT NOT NULL,
    backfill_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Phase 6: the executable simulation of a frozen family.  Evidence, so
-- immutable: a simulation whose trades can be edited after the robustness
-- report read them proves nothing.
CREATE TABLE IF NOT EXISTS intraday_strategy_simulations (
    id BIGSERIAL PRIMARY KEY,
    family_id BIGINT NOT NULL REFERENCES intraday_strategy_families(id) ON DELETE RESTRICT,
    factor_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    recipe_hash TEXT NOT NULL,
    cost_calibration_id BIGINT,
    capital NUMERIC NOT NULL,
    trade_count INTEGER NOT NULL,
    observations INTEGER NOT NULL,
    fill_rate NUMERIC,
    execution_passed BOOLEAN NOT NULL,
    robustness_passed BOOLEAN NOT NULL,
    execution_report JSONB NOT NULL,
    robustness_report JSONB NOT NULL,
    trades JSONB NOT NULL,
    simulation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_strategy_simulations_timeframe_check
        CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_strategy_simulations_capital_check CHECK (capital > 0)
);

CREATE INDEX IF NOT EXISTS intraday_strategy_simulations_family_idx
    ON intraday_strategy_simulations(family_id, created_at DESC);

DROP TRIGGER IF EXISTS intraday_strategy_simulations_immutable_trigger
    ON intraday_strategy_simulations;
CREATE TRIGGER intraday_strategy_simulations_immutable_trigger
    BEFORE UPDATE OR DELETE ON intraday_strategy_simulations
    FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation();

-- Phase 7: paper fills annotated with what the market showed at decision time.
--
-- The fill calibration needs the midpoint and quote *as of the decision*, not
-- just the price that came back. Quoted spread is what the market advertised;
-- slippage is what it charged, and only the second says whether a confirmed
-- edge survives execution. The existing paper_fills table records the fill but
-- not the counterfactual, so it cannot answer the question.
--
-- This table stays empty until a family is actually deployed to paper. That is
-- the correct state: an unfed calibration must report that execution quality is
-- unmeasured, never a pass.
CREATE TABLE IF NOT EXISTS intraday_paper_fill_observations (
    id BIGSERIAL PRIMARY KEY,
    family_id BIGINT REFERENCES intraday_strategy_families(id) ON DELETE RESTRICT,
    factor_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    decision_timestamp TIMESTAMPTZ NOT NULL,
    filled_at TIMESTAMPTZ NOT NULL,
    filled_price NUMERIC NOT NULL,
    midpoint_at_decision NUMERIC NOT NULL,
    bid NUMERIC NOT NULL,
    ask NUMERIC NOT NULL,
    quantity NUMERIC NOT NULL,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_paper_fill_side_check CHECK (side IN ('long', 'short')),
    CONSTRAINT intraday_paper_fill_price_check CHECK (
        filled_price > 0 AND midpoint_at_decision > 0 AND bid > 0 AND ask >= bid
    ),
    -- A fill cannot precede the decision that caused it.
    CONSTRAINT intraday_paper_fill_ordering_check CHECK (filled_at >= decision_timestamp)
);

CREATE INDEX IF NOT EXISTS intraday_paper_fill_observations_factor_idx
    ON intraday_paper_fill_observations(factor_key, decision_timestamp);
