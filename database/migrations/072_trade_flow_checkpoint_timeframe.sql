-- Trade-flow checkpoints must be scoped by timeframe.
--
-- The original checkpoint key was (symbol, session_date, feed), which made a
-- completed 30m trade-flow session suppress later 15m ingestion for the same
-- symbol/session. Existing rows were produced by the 30m pipeline, so they are
-- backfilled as 30m before the key is widened.

ALTER TABLE intraday_trade_ingest_checkpoints
    ADD COLUMN IF NOT EXISTS timeframe TEXT;

UPDATE intraday_trade_ingest_checkpoints
SET timeframe = '30m'
WHERE timeframe IS NULL;

ALTER TABLE intraday_trade_ingest_checkpoints
    ALTER COLUMN timeframe SET NOT NULL;

ALTER TABLE intraday_trade_ingest_checkpoints
    DROP CONSTRAINT IF EXISTS intraday_trade_ingest_checkpoints_pkey;

ALTER TABLE intraday_trade_ingest_checkpoints
    ADD PRIMARY KEY (symbol, session_date, feed, timeframe);

CREATE INDEX IF NOT EXISTS intraday_trade_ingest_checkpoints_timeframe_status_idx
    ON intraday_trade_ingest_checkpoints(timeframe, status, updated_at DESC);
