-- Alpaca/Benzinga news side channel for intraday research.
--
-- Raw articles are stored per symbol and per known-at version.  Historical
-- research must filter on known_at <= decision_timestamp; using updated_at as
-- known_at prevents an older decision from seeing a later article revision.

CREATE TABLE IF NOT EXISTS intraday_news_articles (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    article_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    known_at TIMESTAMPTZ NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    source TEXT,
    url TEXT,
    author TEXT,
    symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    images JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_news_article_hash_check CHECK (length(content_hash) = 64),
    CONSTRAINT intraday_news_article_known_order_check CHECK (known_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS intraday_news_articles_unique_version_idx
    ON intraday_news_articles(provider, article_id, symbol, known_at);

CREATE INDEX IF NOT EXISTS intraday_news_articles_symbol_known_idx
    ON intraday_news_articles(symbol, known_at);

CREATE INDEX IF NOT EXISTS intraday_news_articles_article_idx
    ON intraday_news_articles(provider, article_id);

CREATE TABLE IF NOT EXISTS intraday_news_ingest_checkpoints (
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    articles_seen INTEGER NOT NULL DEFAULT 0,
    articles_upserted INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, symbol, start_at, end_at),
    CONSTRAINT intraday_news_ingest_status_check
        CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT intraday_news_ingest_pages_check CHECK (pages >= 0),
    CONSTRAINT intraday_news_ingest_seen_check CHECK (articles_seen >= 0),
    CONSTRAINT intraday_news_ingest_upserted_check CHECK (articles_upserted >= 0)
);

CREATE TABLE IF NOT EXISTS intraday_news_feature_snapshots (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    provider TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe, timestamp, provider, feature_version),
    CONSTRAINT intraday_news_feature_timeframe_check
        CHECK (timeframe IN ('1m', '15m', '30m'))
);

CREATE INDEX IF NOT EXISTS intraday_news_feature_lookup_idx
    ON intraday_news_feature_snapshots(provider, feature_version, timeframe, timestamp);
