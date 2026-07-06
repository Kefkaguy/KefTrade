ALTER TABLE backtests
ADD COLUMN IF NOT EXISTS strategy_parameters JSONB;

UPDATE backtests
SET strategy_parameters = '{}'::jsonb
WHERE strategy_parameters IS NULL;

ALTER TABLE backtests
ALTER COLUMN strategy_parameters SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_open_positive_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_open_positive_check CHECK (open > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_high_positive_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_high_positive_check CHECK (high > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_low_positive_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_low_positive_check CHECK (low > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_close_positive_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_close_positive_check CHECK (close > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_volume_nonnegative_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_volume_nonnegative_check CHECK (volume >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_high_low_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_high_low_check CHECK (high >= low);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_open_range_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_open_range_check CHECK (open BETWEEN low AND high);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_close_range_check'
    ) THEN
        ALTER TABLE candles ADD CONSTRAINT candles_close_range_check CHECK (close BETWEEN low AND high);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'signals_unique_generation'
    ) THEN
        ALTER TABLE signals ADD CONSTRAINT signals_unique_generation UNIQUE(symbol, timeframe, strategy_name, strategy_version, generated_at);
    END IF;
END $$;
