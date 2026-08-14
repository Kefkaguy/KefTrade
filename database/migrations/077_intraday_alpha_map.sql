-- Alpha cartography: measure information before constructing a strategy.
--
-- Every intraday research table before this one describes a *strategy*: a
-- factor with a threshold, a direction, a holding period, an entry rule.  That
-- ordering is the defect this migration addresses.  Choosing a threshold and a
-- holding period is already a modelling decision, and making it before anyone
-- has measured where the information lives means a dead hypothesis can consume
-- a simulation, a qualification run, a calibration, and a Paper Lab session
-- before anything reports that there was never a forecast to trade.
--
-- These tables store the prior step.  A cell is one (feature, horizon, slice)
-- measurement of forward return: no threshold, no entry rule, no position, no
-- P/L.  It answers "does this feature predict anything, at what horizon, for
-- whom, by how many bps, and does that survive the cost of trading it" and
-- nothing else.  Strategy construction is authorized by a cell verdict, never
-- the other way around.
--
-- The evidence tables are immutable for the same reason the event-study tables
-- are: a measurement that can be re-run after seeing its own result is not a
-- measurement, it is a search.  Re-measuring requires a new declaration, and
-- the new declaration counts toward the multiple-testing correction.

CREATE TABLE IF NOT EXISTS intraday_alpha_map_declarations (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    -- The timeframe features are read at.  Forward returns are measured on
    -- `grid_timeframe`, which is finer, because the horizon question cannot be
    -- answered on the same grid that produced the feature.
    signal_timeframe TEXT NOT NULL,
    grid_timeframe TEXT NOT NULL,
    symbols JSONB NOT NULL,
    features JSONB NOT NULL,
    horizons_seconds JSONB NOT NULL,
    slices JSONB NOT NULL,
    cost_model JSONB NOT NULL,
    -- Required gross edge is cost * safety_multiple.  A candidate whose
    -- measured edge merely matches its modelled cost is not a candidate; the
    -- cost model itself has more estimation error than that.
    cost_safety_multiple NUMERIC NOT NULL,
    split_boundaries JSONB NOT NULL,
    specification JSONB NOT NULL,
    specification_hash TEXT NOT NULL UNIQUE,
    declared_cell_count INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_alpha_map_declaration_hash_check
        CHECK (length(specification_hash) = 64),
    CONSTRAINT intraday_alpha_map_declaration_cells_check
        CHECK (declared_cell_count > 0),
    CONSTRAINT intraday_alpha_map_declaration_safety_check
        CHECK (cost_safety_multiple >= 1),
    CONSTRAINT intraday_alpha_map_declaration_immutable_check CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS intraday_alpha_map_runs (
    id BIGSERIAL PRIMARY KEY,
    declaration_id BIGINT NOT NULL UNIQUE
        REFERENCES intraday_alpha_map_declarations(id) ON DELETE RESTRICT,
    dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    -- The whole grid counts, not the cell that happened to win.  Measuring 9
    -- horizons across 14 features is 126 looks at the same data whether or not
    -- the researcher reports 126 numbers.
    effective_trials INTEGER NOT NULL,
    -- Probability of backtest overfitting from combinatorially symmetric
    -- cross-validation over session blocks.  High PBO means the ranking of
    -- cells does not survive re-partitioning, so the best cell is a selection
    -- artefact regardless of its own t-statistic.
    probability_of_backtest_overfitting NUMERIC,
    cross_sectional_dependence JSONB NOT NULL,
    results JSONB NOT NULL,
    survivors JSONB NOT NULL,
    strategy_construction_authorized BOOLEAN NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_alpha_map_run_phase_check
        CHECK (phase IN ('discovery', 'validation', 'confirmation')),
    CONSTRAINT intraday_alpha_map_run_trials_check CHECK (effective_trials > 0),
    CONSTRAINT intraday_alpha_map_run_observations_check CHECK (observation_count >= 0),
    CONSTRAINT intraday_alpha_map_run_pbo_check
        CHECK (probability_of_backtest_overfitting IS NULL
               OR (probability_of_backtest_overfitting >= 0
                   AND probability_of_backtest_overfitting <= 1)),
    CONSTRAINT intraday_alpha_map_run_immutable_check CHECK (immutable = TRUE)
);

-- One row per (feature, horizon, slice).  Stored individually rather than only
-- inside the run's JSON so the horizon profile of a feature can be queried
-- directly -- "show me every horizon at which signed_trade_imbalance predicts
-- anything" is the question this whole layer exists to answer cheaply.
CREATE TABLE IF NOT EXISTS intraday_alpha_map_cells (
    run_id BIGINT NOT NULL
        REFERENCES intraday_alpha_map_runs(id) ON DELETE RESTRICT,
    cell_key TEXT NOT NULL,
    feature TEXT NOT NULL,
    feature_transform TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    slice_kind TEXT NOT NULL,
    slice_value TEXT NOT NULL,
    observations INTEGER NOT NULL,
    distinct_sessions INTEGER NOT NULL,
    distinct_symbols INTEGER NOT NULL,
    rank_ic NUMERIC,
    rank_ic_t_statistic NUMERIC,
    extreme_bucket_gross_bps NUMERIC,
    long_short_gross_bps NUMERIC,
    estimated_round_trip_cost_bps NUMERIC,
    required_gross_bps NUMERIC,
    net_bps NUMERIC,
    monotonicity NUMERIC,
    -- The whole point of the layer: a cell reports why it died, and the
    -- reasons are distinguishable.  "No information" and "information that
    -- costs more to harvest than it pays" are different findings with
    -- different follow-ups, and collapsing them into "strategy rejected" is
    -- what makes a research programme repeat itself.
    verdict TEXT NOT NULL,
    detail JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (run_id, cell_key),
    CONSTRAINT intraday_alpha_map_cell_verdict_check
        CHECK (verdict IN (
            'insufficient_data',
            'no_information',
            'information_below_cost',
            'unstable',
            'tradable_candidate'
        )),
    CONSTRAINT intraday_alpha_map_cell_horizon_check CHECK (horizon_seconds > 0),
    CONSTRAINT intraday_alpha_map_cell_observations_check CHECK (observations >= 0),
    CONSTRAINT intraday_alpha_map_cell_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_alpha_map_cells_feature_idx
    ON intraday_alpha_map_cells(feature, horizon_seconds, verdict);

CREATE INDEX IF NOT EXISTS intraday_alpha_map_cells_verdict_idx
    ON intraday_alpha_map_cells(verdict, created_at DESC);

CREATE INDEX IF NOT EXISTS intraday_alpha_map_runs_dataset_idx
    ON intraday_alpha_map_runs(dataset_id, created_at DESC);

DO $$
DECLARE
    evidence_table TEXT;
BEGIN
    FOREACH evidence_table IN ARRAY ARRAY[
        'intraday_alpha_map_declarations',
        'intraday_alpha_map_runs',
        'intraday_alpha_map_cells'
    ]
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON %I',
            evidence_table || '_immutable_trigger',
            evidence_table
        );
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation()',
            evidence_table || '_immutable_trigger',
            evidence_table
        );
    END LOOP;
END;
$$;

-- Paper Lab is downstream evidence, not a microscope.
--
-- An experiment now records which alpha-map cell authorized it.  A curiosity
-- run is still allowed -- the lab exists partly to shake out broker semantics
-- and scheduling, which needs no edge at all -- but it must say so in a column
-- rather than by omission, so that a later reader cannot mistake an
-- unauthorized run for a validated one.
ALTER TABLE intraday_paper_lab_experiments
    ADD COLUMN IF NOT EXISTS alpha_map_cell_run_id BIGINT
        REFERENCES intraday_alpha_map_runs(id) ON DELETE RESTRICT;

ALTER TABLE intraday_paper_lab_experiments
    ADD COLUMN IF NOT EXISTS alpha_map_cell_key TEXT;

ALTER TABLE intraday_paper_lab_experiments
    ADD COLUMN IF NOT EXISTS evidence_basis TEXT NOT NULL DEFAULT 'unreviewed';

ALTER TABLE intraday_paper_lab_experiments
    DROP CONSTRAINT IF EXISTS intraday_paper_lab_evidence_basis_check;

ALTER TABLE intraday_paper_lab_experiments
    ADD CONSTRAINT intraday_paper_lab_evidence_basis_check
    CHECK (evidence_basis IN (
        'unreviewed',
        'alpha_map_cleared',
        'operational_curiosity',
        'frozen_failed_hypothesis'
    ));

-- An authorized experiment must name the cell that authorized it.  Without
-- this a caller could set the basis to 'alpha_map_cleared' and point at
-- nothing, which is worse than no column at all.
ALTER TABLE intraday_paper_lab_experiments
    DROP CONSTRAINT IF EXISTS intraday_paper_lab_alpha_map_reference_check;

ALTER TABLE intraday_paper_lab_experiments
    ADD CONSTRAINT intraday_paper_lab_alpha_map_reference_check
    CHECK (
        evidence_basis <> 'alpha_map_cleared'
        OR (alpha_map_cell_run_id IS NOT NULL AND alpha_map_cell_key IS NOT NULL)
    );

-- Freezing an experiment preserves it as a finding.  The failure mode this
-- prevents is re-fitting: an experiment that lost money gets a new threshold
-- and runs again under the same name, and the record of what the original
-- hypothesis predicted disappears.  A frozen experiment keeps its verdict and
-- can no longer be started.
ALTER TABLE intraday_paper_lab_experiments
    ADD COLUMN IF NOT EXISTS frozen_at TIMESTAMPTZ;

ALTER TABLE intraday_paper_lab_experiments
    ADD COLUMN IF NOT EXISTS frozen_verdict JSONB;

CREATE INDEX IF NOT EXISTS intraday_paper_lab_experiments_evidence_idx
    ON intraday_paper_lab_experiments(evidence_basis, trading_date DESC);
