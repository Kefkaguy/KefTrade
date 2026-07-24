-- Phase 12.4 (Intraday Portfolio-Wide Failure Analysis) discovered that
-- research_campaign_jobs.result only ever stored aggregate metrics -- the
-- individual trade list produced inside run_backtest()/evaluate_candidate()
-- was computed in memory and then discarded before the job's result was
-- written. That made trade-level root-cause analysis (exit-reason mix,
-- MFE/MAE, true pre-fee gross P&L, entry timing, position sizing) impossible
-- for Campaign 47. This table gives the backtester somewhere to persist that
-- detail for a *new*, separately-versioned re-run using the exact same
-- strategy families/parameters as Campaign 47 -- Campaign 47's own evidence
-- is left untouched.
--
-- Populated only for intraday-lab candidates (see
-- app.services.labs.intraday.families.registry.is_intraday_lab_candidate);
-- swing research campaigns are unaffected.
CREATE TABLE IF NOT EXISTS research_campaign_trades (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES research_campaign_jobs(id) ON DELETE CASCADE,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    strategy_architecture TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    exit_time TIMESTAMPTZ NOT NULL,
    entry_price NUMERIC NOT NULL,
    exit_price NUMERIC NOT NULL,
    quantity NUMERIC NOT NULL,
    stop_loss NUMERIC NOT NULL,
    take_profit NUMERIC NOT NULL,
    risk_per_unit NUMERIC NOT NULL,
    gross_pnl NUMERIC NOT NULL,
    fees NUMERIC NOT NULL,
    slippage_cost NUMERIC NOT NULL,
    net_pnl NUMERIC NOT NULL,
    pnl_pct DOUBLE PRECISION NOT NULL,
    exit_reason TEXT NOT NULL,
    holding_period_hours DOUBLE PRECISION NOT NULL,
    mfe_amount NUMERIC,
    mae_amount NUMERIC,
    mfe_r DOUBLE PRECISION,
    mae_r DOUBLE PRECISION,
    bars_to_mfe INTEGER,
    bars_to_mae INTEGER,
    entry_minutes_from_open INTEGER,
    entry_minutes_to_close INTEGER,
    entry_session_relative_volume NUMERIC,
    entry_gap_percent NUMERIC,
    market_regime TEXT,
    volatility_regime TEXT,
    month_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_campaign_trades_direction_check CHECK (direction IN ('long', 'short'))
);

CREATE INDEX IF NOT EXISTS research_campaign_trades_job_idx
    ON research_campaign_trades(job_id);

CREATE INDEX IF NOT EXISTS research_campaign_trades_campaign_idx
    ON research_campaign_trades(campaign_id);

CREATE INDEX IF NOT EXISTS research_campaign_trades_campaign_symbol_idx
    ON research_campaign_trades(campaign_id, symbol);

CREATE INDEX IF NOT EXISTS research_campaign_trades_campaign_exit_reason_idx
    ON research_campaign_trades(campaign_id, exit_reason);

CREATE INDEX IF NOT EXISTS research_campaign_trades_campaign_architecture_idx
    ON research_campaign_trades(campaign_id, strategy_architecture);
