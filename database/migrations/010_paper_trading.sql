CREATE TABLE IF NOT EXISTS paper_accounts (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    starting_cash NUMERIC NOT NULL,
    cash_balance NUMERIC NOT NULL,
    realized_pnl NUMERIC NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT paper_accounts_starting_cash_check CHECK (starting_cash > 0),
    CONSTRAINT paper_accounts_cash_nonnegative_check CHECK (cash_balance >= 0),
    CONSTRAINT paper_accounts_status_check CHECK (status IN ('active', 'paused', 'closed')),
    CONSTRAINT paper_accounts_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS strategy_deployments (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL DEFAULT 'v1',
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'created',
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paused_at TIMESTAMPTZ,
    CONSTRAINT strategy_deployments_status_check CHECK (status IN ('created', 'active', 'paused', 'archived')),
    CONSTRAINT strategy_deployments_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    deployment_id BIGINT REFERENCES strategy_deployments(id) ON DELETE SET NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '1d',
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    limit_price NUMERIC,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at TIMESTAMPTZ,
    rejected_reason TEXT,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT paper_orders_side_check CHECK (side IN ('buy', 'sell')),
    CONSTRAINT paper_orders_type_check CHECK (order_type IN ('market', 'limit')),
    CONSTRAINT paper_orders_status_check CHECK (status IN ('pending', 'rejected', 'filled', 'canceled')),
    CONSTRAINT paper_orders_quantity_check CHECK (quantity > 0),
    CONSTRAINT paper_orders_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES paper_orders(id) ON DELETE CASCADE,
    account_id BIGINT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    fill_price NUMERIC NOT NULL,
    gross_amount NUMERIC NOT NULL,
    fee NUMERIC NOT NULL DEFAULT 0,
    slippage NUMERIC NOT NULL DEFAULT 0,
    filled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    candle_timestamp TIMESTAMPTZ,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(order_id),
    CONSTRAINT paper_fills_quantity_check CHECK (quantity > 0),
    CONSTRAINT paper_fills_price_check CHECK (fill_price > 0),
    CONSTRAINT paper_fills_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    quantity NUMERIC NOT NULL DEFAULT 0,
    average_price NUMERIC NOT NULL DEFAULT 0,
    realized_pnl NUMERIC NOT NULL DEFAULT 0,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(account_id, symbol),
    CONSTRAINT paper_positions_long_only_check CHECK (quantity >= 0),
    CONSTRAINT paper_positions_average_price_check CHECK (average_price >= 0),
    CONSTRAINT paper_positions_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS paper_equity_curve (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cash_balance NUMERIC NOT NULL,
    equity NUMERIC NOT NULL,
    unrealized_pnl NUMERIC NOT NULL DEFAULT 0,
    realized_pnl NUMERIC NOT NULL DEFAULT 0,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT paper_equity_curve_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES paper_accounts(id) ON DELETE SET NULL,
    deployment_id BIGINT REFERENCES strategy_deployments(id) ON DELETE SET NULL,
    order_id BIGINT REFERENCES paper_orders(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT execution_logs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS paper_orders_account_status_idx ON paper_orders(account_id, status);
CREATE INDEX IF NOT EXISTS paper_fills_account_symbol_idx ON paper_fills(account_id, symbol);
CREATE INDEX IF NOT EXISTS paper_positions_account_idx ON paper_positions(account_id);
CREATE INDEX IF NOT EXISTS paper_equity_curve_account_timestamp_idx ON paper_equity_curve(account_id, timestamp);
CREATE INDEX IF NOT EXISTS strategy_deployments_account_status_idx ON strategy_deployments(account_id, status);
