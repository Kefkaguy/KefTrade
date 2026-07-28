-- 059_signal_diagnostics_job_progress.sql
-- Adds lightweight progress reporting for background signal diagnostic jobs.

ALTER TABLE research_signal_diagnostics_jobs
    ADD COLUMN IF NOT EXISTS progress_total INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_completed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_current TEXT;
