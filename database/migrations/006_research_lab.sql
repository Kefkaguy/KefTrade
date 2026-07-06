CREATE TABLE IF NOT EXISTS research_hypotheses (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_hypotheses_status_check CHECK (status IN ('active', 'rejected', 'research_more', 'candidate_for_paper_trading', 'validated'))
);

CREATE INDEX IF NOT EXISTS research_hypotheses_created_at_idx
    ON research_hypotheses(created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_experiments (
    id BIGSERIAL PRIMARY KEY,
    hypothesis_id BIGINT REFERENCES research_hypotheses(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    dataset JSONB NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    parameters JSONB NOT NULL,
    comparison_plan JSONB NOT NULL,
    evidence_rules JSONB NOT NULL,
    result JSONB NOT NULL,
    recommendation TEXT NOT NULL,
    markdown_report TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS strategy_experiments_hypothesis_created_at_idx
    ON strategy_experiments(hypothesis_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_journal_entries (
    id BIGSERIAL PRIMARY KEY,
    hypothesis_id BIGINT REFERENCES research_hypotheses(id) ON DELETE SET NULL,
    experiment_id BIGINT REFERENCES strategy_experiments(id) ON DELETE SET NULL,
    entry_type TEXT NOT NULL,
    dataset JSONB NOT NULL,
    parameters JSONB NOT NULL,
    results JSONB NOT NULL,
    conclusion TEXT NOT NULL,
    next_actions JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_journal_entries_type_check CHECK (entry_type IN ('hypothesis_created', 'experiment_run', 'failure_analysis', 'edge_discovery', 'strategy_evolution'))
);

CREATE INDEX IF NOT EXISTS research_journal_entries_created_at_idx
    ON research_journal_entries(created_at DESC);
