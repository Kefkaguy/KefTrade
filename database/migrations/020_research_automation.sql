CREATE TABLE IF NOT EXISTS research_automation_queue (
    id BIGSERIAL PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 50,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    latest_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_automation_queue_status_check CHECK (status IN ('queued', 'running', 'completed', 'failed', 'skipped')),
    CONSTRAINT research_automation_queue_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_automation_queue_status_priority_idx
    ON research_automation_queue(status, priority, created_at);

CREATE TABLE IF NOT EXISTS research_automation_runs (
    id BIGSERIAL PRIMARY KEY,
    queue_id BIGINT REFERENCES research_automation_queue(id) ON DELETE SET NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    result JSONB NOT NULL,
    generated_hypothesis JSONB NOT NULL,
    objective_metrics JSONB NOT NULL,
    automation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_automation_runs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_automation_runs_symbol_strategy_idx
    ON research_automation_runs(symbol, timeframe, strategy_name, created_at DESC);
