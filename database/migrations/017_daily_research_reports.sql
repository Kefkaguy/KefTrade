CREATE TABLE IF NOT EXISTS daily_research_reports (
    id BIGSERIAL PRIMARY KEY,
    report_date DATE NOT NULL UNIQUE,
    summary JSONB NOT NULL,
    markdown_report TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT daily_research_reports_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS daily_research_reports_date_idx
    ON daily_research_reports(report_date DESC);
