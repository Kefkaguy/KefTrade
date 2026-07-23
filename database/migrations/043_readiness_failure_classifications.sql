-- 043_readiness_failure_classifications.sql
-- Allow the data-readiness classifications the preflight actually emits.
--
-- Campaign 37 (hidden-gem recovery) lost all 270 of its 1d jobs to a constraint
-- violation: with zero 1d candles the preflight classifies the block as
-- 'missing_dataset', which the failure_classification CHECK did not permit.
-- The insert failed, the job was recorded as a generic 'unknown_error', and the
-- real reason ("no 1d data for this symbol") was hidden.
--
-- These five classifications are produced by data_readiness_for_job and are
-- legitimate, diagnostic block reasons -- they belong in the allowed set.

ALTER TABLE research_campaign_jobs
    DROP CONSTRAINT IF EXISTS research_campaign_jobs_failure_classification_check;

ALTER TABLE research_campaign_jobs
    ADD CONSTRAINT research_campaign_jobs_failure_classification_check
    CHECK (
        failure_classification IS NULL OR failure_classification IN (
            'data_unavailable',
            'stale_data',
            'provider_error',
            'validation_error',
            'strategy_error',
            'database_error',
            'worker_timeout',
            'unknown_error',
            'rate_limit',
            'budget_exhausted',
            -- data-readiness block reasons
            'missing_dataset',
            'insufficient_historical_depth',
            'unsupported_symbol',
            'unsupported_timeframe',
            'feature_generation_failure',
            'blocked_terminal'
        )
    );
