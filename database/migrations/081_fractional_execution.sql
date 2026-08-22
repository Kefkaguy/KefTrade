-- Fractional-share execution capability for Alpaca paper.
--
-- Two facts this migration records, both of which were previously unavailable
-- to the execution path.
--
-- 1. Fractional eligibility is a per-asset property published by Alpaca. The
--    assets importer already *parsed* `fractionable` and then dropped it on the
--    floor: `import_alpaca_stock_assets` writes into `symbols` without it. An
--    execution gate that must refuse a non-fractionable name therefore had no
--    source of truth to consult. The column is nullable on purpose --
--    "we have never asked Alpaca about this symbol" is a different fact from
--    "Alpaca says this symbol cannot be traded fractionally", and only the
--    second one is a decision. NULL fails closed at the gate.
--
-- 2. Fractional execution is opt-in per deployment configuration, recorded
--    against a *new* risk policy version. The existing `phase10-risk-v1`
--    policy is byte-frozen: `persist_policy` re-hashes the policy dict and
--    raises on any drift, so an already-approved deployment keeps the exact
--    whole-share policy it was approved under. Nothing here changes an
--    existing frozen configuration.
--
-- This migration adds no execution capability by itself. Both execution flags
-- remain off, and the share policy defaults to whole shares everywhere.

ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS fractionable BOOLEAN,
    ADD COLUMN IF NOT EXISTS fractionable_checked_at TIMESTAMPTZ;

COMMENT ON COLUMN symbols.fractionable IS
    'Alpaca asset fractionable flag. NULL means never observed, which fails '
    'closed at the fractional execution gate.';

-- ---------------------------------------------------------------------------
-- Portfolio sizing plans -- observe-only evidence, never an order
-- ---------------------------------------------------------------------------
--
-- A sizing plan is the outcome-blind record of "what would we have submitted".
-- It is deliberately NOT `proposed_broker_orders`: that table is single-symbol,
-- requires a positive `stop_price`, and is bound to one signal. An equal-weight
-- portfolio has neither a per-name stop nor one signal, and forcing it into
-- that shape would have meant inventing stops.
--
-- Rows here can never become orders on their own. Submission still goes through
-- the existing gated path, which requires both execution flags.

CREATE TABLE IF NOT EXISTS portfolio_sizing_plans (
    id BIGSERIAL PRIMARY KEY,
    plan_key TEXT NOT NULL UNIQUE,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    broker_account_id BIGINT REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    risk_policy_version_id BIGINT REFERENCES risk_policy_versions(id) ON DELETE RESTRICT,
    share_policy TEXT NOT NULL,
    portfolio_capital NUMERIC NOT NULL,
    selected_count INTEGER NOT NULL,
    target_dollars_per_name NUMERIC NOT NULL,
    total_requested_notional NUMERIC NOT NULL,
    estimated_residual_cash NUMERIC NOT NULL,
    fractionable_count INTEGER NOT NULL,
    nonfractionable_count INTEGER NOT NULL,
    max_absolute_weight_error NUMERIC NOT NULL,
    max_relative_weight_error NUMERIC NOT NULL,
    blocked BOOLEAN NOT NULL,
    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocked_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    orders JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    observe_only BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT portfolio_sizing_share_policy_check
        CHECK (share_policy IN ('whole_shares', 'fractional_qty', 'notional')),
    CONSTRAINT portfolio_sizing_positive_check
        CHECK (portfolio_capital > 0 AND selected_count > 0),
    CONSTRAINT portfolio_sizing_nonnegative_check
        CHECK (total_requested_notional >= 0 AND estimated_residual_cash >= 0),
    -- Observe-only is not a default that can be turned off by an UPDATE: a plan
    -- is evidence about a portfolio, and the moment one could authorize a
    -- mutation it would be a second, ungoverned order path.
    CONSTRAINT portfolio_sizing_observe_only_check CHECK (observe_only = TRUE)
);

CREATE INDEX IF NOT EXISTS portfolio_sizing_plans_strategy_idx
    ON portfolio_sizing_plans(strategy_name, strategy_version, created_at DESC);

-- ---------------------------------------------------------------------------
-- Portfolio rebalance plans -- the observe-only bridge for frozen signals
-- ---------------------------------------------------------------------------
--
-- MOM_12_1 does not fit `proposed_broker_orders`: that table is one symbol per
-- row bound to one signal, and requires a positive `stop_price`. An equal-weight
-- monthly book has neither a per-name stop nor one signal per name, and
-- inventing stops to satisfy the constraint would have invented risk parameters
-- the research never had.
--
-- `provenance` separates a genuine forward signal from a historical CSV
-- replayed for plumbing validation. The two are indistinguishable on disk, so
-- the distinction is recorded rather than inferred, and a replay can never be
-- promoted to forward evidence by accident.

CREATE TABLE IF NOT EXISTS portfolio_rebalance_plans (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    universe_hash TEXT NOT NULL,
    signal_date DATE NOT NULL,
    intended_execution_date DATE NOT NULL,
    provenance TEXT NOT NULL,
    signal_source_path TEXT NOT NULL,
    signal_source_sha256 TEXT NOT NULL,
    broker_account_id BIGINT REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    risk_policy_version_id BIGINT REFERENCES risk_policy_versions(id) ON DELETE RESTRICT,
    share_policy TEXT NOT NULL,
    allocated_capital NUMERIC NOT NULL,
    selected_count INTEGER NOT NULL,
    target_weight NUMERIC NOT NULL,
    target_dollars_per_name NUMERIC NOT NULL,
    total_target_notional NUMERIC NOT NULL,
    residual_cash NUMERIC NOT NULL,
    reconciliation_status TEXT NOT NULL,
    blocked BOOLEAN NOT NULL,
    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocked_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    symbol_plans JSONB NOT NULL DEFAULT '[]'::jsonb,
    exits JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification JSONB NOT NULL DEFAULT '{}'::jsonb,
    observe_only BOOLEAN NOT NULL DEFAULT TRUE,
    orders_submitted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT rebalance_plan_provenance_check
        CHECK (provenance IN ('forward', 'test_replay')),
    CONSTRAINT rebalance_plan_share_policy_check
        CHECK (share_policy IN ('whole_shares', 'fractional_qty', 'notional')),
    CONSTRAINT rebalance_plan_positive_check
        CHECK (allocated_capital > 0 AND selected_count > 0 AND target_weight > 0),
    CONSTRAINT rebalance_plan_within_capital_check
        CHECK (total_target_notional <= allocated_capital),
    CONSTRAINT rebalance_plan_residual_check CHECK (residual_cash >= 0),
    -- A plan is evidence about a portfolio. The moment one could authorise a
    -- mutation it would be a second, ungoverned order path alongside the
    -- existing gated one.
    CONSTRAINT rebalance_plan_observe_only_check CHECK (observe_only = TRUE),
    CONSTRAINT rebalance_plan_not_submitted_check CHECK (orders_submitted = FALSE),
    UNIQUE (strategy, signal_date, provenance, signal_source_sha256)
);

CREATE INDEX IF NOT EXISTS portfolio_rebalance_plans_lookup_idx
    ON portfolio_rebalance_plans(strategy, signal_date DESC, provenance);
