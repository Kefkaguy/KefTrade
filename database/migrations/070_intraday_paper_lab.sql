-- Isolated Alpaca Paper lab for unqualified intraday factor curiosity tests.
--
-- This is deliberately separate from elite/external paper deployment tables.
-- It is for fake-money experiments only and carries its own audit trail.

CREATE TABLE IF NOT EXISTS intraday_paper_lab_experiments (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    trading_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    factor_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    calibration_id BIGINT REFERENCES intraday_trade_imbalance_calibrations(id) ON DELETE RESTRICT,
    threshold NUMERIC NOT NULL,
    symbols JSONB NOT NULL,
    config JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_paper_lab_status_check
        CHECK (status IN ('created', 'running', 'stopped', 'completed', 'failed')),
    CONSTRAINT intraday_paper_lab_timeframe_check CHECK (timeframe = '30m'),
    CONSTRAINT intraday_paper_lab_threshold_check CHECK (threshold > 0 AND threshold <= 1)
);

CREATE TABLE IF NOT EXISTS intraday_paper_lab_decisions (
    id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL
        REFERENCES intraday_paper_lab_experiments(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    signal_bar_start TIMESTAMPTZ NOT NULL,
    signal_bar_end TIMESTAMPTZ NOT NULL,
    side TEXT,
    signed_trade_imbalance NUMERIC,
    trade_count INTEGER,
    unclassified_share NUMERIC,
    effective_trade_count NUMERIC,
    action TEXT NOT NULL,
    reason TEXT,
    client_order_id TEXT,
    broker_order_id TEXT,
    broker_status TEXT,
    request_payload JSONB,
    response_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_paper_lab_decision_side_check
        CHECK (side IS NULL OR side IN ('buy', 'sell')),
    CONSTRAINT intraday_paper_lab_decision_action_check
        CHECK (action IN ('enter', 'skip', 'exit', 'flatten', 'error')),
    UNIQUE (experiment_id, symbol, signal_bar_start, action)
);

CREATE TABLE IF NOT EXISTS intraday_paper_lab_positions (
    id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL
        REFERENCES intraday_paper_lab_experiments(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    entry_decision_id BIGINT
        REFERENCES intraday_paper_lab_decisions(id) ON DELETE SET NULL,
    entry_client_order_id TEXT,
    entry_broker_order_id TEXT,
    signal_bar_start TIMESTAMPTZ NOT NULL,
    exit_due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_decision_id BIGINT
        REFERENCES intraday_paper_lab_decisions(id) ON DELETE SET NULL,
    exit_client_order_id TEXT,
    exit_broker_order_id TEXT,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    CONSTRAINT intraday_paper_lab_position_side_check CHECK (side IN ('long', 'short')),
    CONSTRAINT intraday_paper_lab_position_quantity_check CHECK (quantity > 0),
    CONSTRAINT intraday_paper_lab_position_status_check
        CHECK (status IN ('open', 'closing', 'closed', 'rejected'))
);

CREATE INDEX IF NOT EXISTS intraday_paper_lab_decisions_experiment_created_idx
    ON intraday_paper_lab_decisions(experiment_id, created_at DESC);

CREATE INDEX IF NOT EXISTS intraday_paper_lab_positions_experiment_status_idx
    ON intraday_paper_lab_positions(experiment_id, status, symbol);
