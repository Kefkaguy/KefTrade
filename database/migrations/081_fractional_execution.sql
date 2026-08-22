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

-- ---------------------------------------------------------------------------
-- Strategy ownership ledger
-- ---------------------------------------------------------------------------
--
-- Alpaca reports one position book per account. That book answers "does this
-- account hold AAPL", which is not the question a rebalance asks. The question
-- is "does MOM_12_1 hold AAPL", and no broker endpoint can answer it, because
-- the attribution exists only here.
--
-- Without this table the only available reading of "held" is the account book,
-- and a rebalance would liquidate a manual position, another strategy's
-- position, or a legacy holding, simply because the symbol left the signal.
-- Absence of a row is therefore not zero-with-confidence -- it is no evidence,
-- and the bridge blocks rather than assuming either way.
CREATE TABLE IF NOT EXISTS strategy_owned_positions (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    -- NUMERIC(20, 9) matches the fractional quantity precision used throughout
    -- this migration; a share count is never a float.
    quantity NUMERIC(20, 9) NOT NULL,
    average_entry_price NUMERIC(20, 9),
    -- The last time this attribution was established. Read as evidence with a
    -- timestamp, never as a standing fact.
    as_of TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Which reconciliation run last agreed the attribution matches the broker.
    reconciliation_run_id BIGINT REFERENCES broker_reconciliation_runs(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'reconciliation',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- A negative attribution would be a short by bookkeeping. The whole sell
    -- path exists to make shorts unreachable, so they are unrepresentable here.
    CONSTRAINT strategy_owned_positions_non_negative_check CHECK (quantity >= 0),
    CONSTRAINT strategy_owned_positions_price_check
        CHECK (average_entry_price IS NULL OR average_entry_price > 0),
    UNIQUE (strategy, broker_account_id, symbol)
);

CREATE INDEX IF NOT EXISTS strategy_owned_positions_lookup_idx
    ON strategy_owned_positions(strategy, broker_account_id)
    WHERE quantity > 0;

-- ---------------------------------------------------------------------------
-- Ownership lifecycle: attribution in, confirmed fills only
-- ---------------------------------------------------------------------------
--
-- Two tables, because attribution and quantity are different kinds of evidence
-- and must never substitute for one another.
--
-- strategy_order_attributions answers "whose order was this". It is written
-- when the order is planned, and carries NO quantity: the moment it did,
-- submitted size would become ownership evidence, and a strategy would own
-- shares the market never gave it.
--
-- strategy_ownership_events is the applied-fill log. Its unique key is the
-- broker's own activity id, so replaying broker activity -- after a restart, a
-- backfill, or a duplicated page -- cannot apply a fill twice.
CREATE TABLE IF NOT EXISTS strategy_order_attributions (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    client_order_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    intended_side TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT strategy_order_attributions_side_check
        CHECK (intended_side IN ('buy', 'sell')),
    CONSTRAINT strategy_order_attributions_version_check
        CHECK (length(strategy_version) > 0),
    -- The client order id is deterministic per (strategy, version, rebalance,
    -- symbol), so a retried rebalance re-attributes the same order rather than
    -- claiming a second one.
    UNIQUE (broker_account_id, client_order_id)
);

CREATE TABLE IF NOT EXISTS strategy_ownership_events (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    -- Alpaca's broker_activity_id. Not derived by us: a key we computed would
    -- change whenever the derivation changed, and every past fill would apply
    -- again the next time we replayed.
    fill_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    side TEXT NOT NULL,
    filled_quantity NUMERIC(20, 9) NOT NULL,
    fill_price NUMERIC(20, 9) NOT NULL,
    quantity_delta NUMERIC(20, 9) NOT NULL,
    resulting_quantity NUMERIC(20, 9) NOT NULL,
    -- Which synchronization cycle applied this fill. Provenance only: the sync
    -- run explains when we learned of the fill, never how much of it happened.
    sync_run_id BIGINT REFERENCES broker_sync_runs(id) ON DELETE SET NULL,
    transaction_at TIMESTAMPTZ NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT strategy_ownership_events_side_check CHECK (side IN ('buy', 'sell')),
    CONSTRAINT strategy_ownership_events_filled_check CHECK (filled_quantity > 0),
    CONSTRAINT strategy_ownership_events_price_check CHECK (fill_price > 0),
    -- Attribution can never go negative, here or in the aggregate.
    CONSTRAINT strategy_ownership_events_resulting_check CHECK (resulting_quantity >= 0),
    -- The whole of the idempotency guarantee.
    UNIQUE (broker_account_id, fill_id)
);

CREATE INDEX IF NOT EXISTS strategy_ownership_events_replay_idx
    ON strategy_ownership_events(strategy, broker_account_id, transaction_at, fill_id);
