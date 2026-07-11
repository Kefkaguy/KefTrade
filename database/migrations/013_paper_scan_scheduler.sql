CREATE TABLE IF NOT EXISTS paper_scan_scheduler (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    cadence TEXT NOT NULL DEFAULT '60m',
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    latest_result TEXT,
    latest_error TEXT,
    is_running BOOLEAN NOT NULL DEFAULT FALSE,
    running_since TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT paper_scan_scheduler_singleton CHECK (id = TRUE),
    CONSTRAINT paper_scan_scheduler_cadence_check CHECK (cadence IN ('manual', '15m', '30m', '60m'))
);

INSERT INTO paper_scan_scheduler(id, enabled, cadence, next_run_at)
VALUES (TRUE, TRUE, '60m', NOW() + INTERVAL '60 minutes')
ON CONFLICT (id) DO NOTHING;
