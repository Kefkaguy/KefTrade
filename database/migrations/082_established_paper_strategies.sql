-- Runtime authority and immutable decisions for the three established Alpaca
-- Paper strategies.  Rows start disabled: migration is schema, never approval.

CREATE TABLE IF NOT EXISTS established_paper_strategies (
    strategy TEXT PRIMARY KEY,
    strategy_version TEXT NOT NULL,
    symbol TEXT,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    enabled_at TIMESTAMPTZ,
    enabled_by TEXT,
    disabled_at TIMESTAMPTZ,
    disabled_by TEXT,
    latest_status TEXT NOT NULL DEFAULT 'disabled',
    latest_error TEXT,
    last_evaluated_session DATE,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT established_strategy_name_check CHECK (
        strategy IN ('SPY_RSI5_SMA200', 'SPY_CONNORS_PULLBACK', 'MOM_12_1')
    )
);

CREATE TABLE IF NOT EXISTS established_paper_strategy_decisions (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL REFERENCES established_paper_strategies(strategy) ON DELETE RESTRICT,
    strategy_version TEXT NOT NULL,
    broker_account_id BIGINT REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    session_date DATE NOT NULL,
    action TEXT NOT NULL,
    decision_key TEXT NOT NULL UNIQUE,
    client_order_id TEXT,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    signal JSONB NOT NULL DEFAULT '{}'::jsonb,
    order_payload JSONB,
    response_payload JSONB,
    error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT established_decision_action_check CHECK (
        action IN ('hold', 'enter_next_open', 'exit_next_open', 'protective_stop', 'mom_rebalance')
    ),
    CONSTRAINT established_decision_status_check CHECK (
        status IN ('observed', 'planned', 'submitted', 'accepted', 'filled', 'canceled', 'refused', 'failed')
    ),
    UNIQUE (strategy, session_date, action)
);

CREATE INDEX IF NOT EXISTS established_decisions_strategy_idx
    ON established_paper_strategy_decisions(strategy, session_date DESC, id DESC);

INSERT INTO established_paper_strategies(strategy, strategy_version, symbol)
VALUES
    ('SPY_RSI5_SMA200', '1.0.0', 'SPY'),
    ('SPY_CONNORS_PULLBACK', '1.0.0', 'SPY'),
    ('MOM_12_1', 'mom_12_1_shadow_v1', NULL)
ON CONFLICT(strategy) DO NOTHING;
