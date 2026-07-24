from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    environment: str = "development"
    log_level: str = "INFO"
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")
    cache_key_prefix: str = Field(default="keftrade", validation_alias="KEFTRADE_CACHE_KEY_PREFIX")
    database_url: str = "postgresql://keftrade:keftrade@127.0.0.1:5432/keftrade"
    cors_origins: str = "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:3001,http://localhost:3001"
    binance_base_url: str = "https://api.binance.us"
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    research_copilot_model: str | None = None
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    alpaca_trading_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_intraday_max_lookback_days: int = 1825
    broker_provider: str = Field(default="alpaca", validation_alias="BROKER_PROVIDER")
    alpaca_paper_base_url: str = Field(default="https://paper-api.alpaca.markets", validation_alias="ALPACA_PAPER_BASE_URL")
    alpaca_paper_api_key: str | None = Field(default=None, validation_alias="ALPACA_PAPER_API_KEY")
    alpaca_paper_secret_key: str | None = Field(default=None, validation_alias="ALPACA_PAPER_SECRET_KEY")
    broker_sync_enabled: bool = Field(default=True, validation_alias="BROKER_SYNC_ENABLED")
    broker_reconciliation_enabled: bool = Field(default=True, validation_alias="BROKER_RECONCILIATION_ENABLED")
    broker_shadow_execution_enabled: bool = Field(default=True, validation_alias="BROKER_SHADOW_EXECUTION_ENABLED")
    broker_order_submission_enabled: bool = Field(default=False, validation_alias="BROKER_ORDER_SUBMISSION_ENABLED")
    external_paper_execution_enabled: bool = Field(default=False, validation_alias="EXTERNAL_PAPER_EXECUTION_ENABLED")
    elite_minimum_trades_per_year: float = Field(default=0, validation_alias="ELITE_MINIMUM_TRADES_PER_YEAR")
    # Phase 12 (Intraday Research Lab), Step 1. Only opening_range_minutes is
    # consumed by Step 1's feature computation
    # (app.services.labs.intraday.features). minimum_distinct_sessions and
    # intraday_cost_multiplier are validation-rule thresholds intended for the
    # Step 4 validation work (not yet implemented) -- they are defined here
    # now, per the Step 1 requirements, so that phase's configuration path
    # already exists and does not require another settings/migration change.
    # Neither affects any computation yet.
    intraday_opening_range_minutes: int = Field(default=30, validation_alias="INTRADAY_OPENING_RANGE_MINUTES")
    intraday_minimum_distinct_sessions: int = Field(default=20, validation_alias="INTRADAY_MINIMUM_DISTINCT_SESSIONS")
    intraday_cost_multiplier: float = Field(default=2.0, validation_alias="INTRADAY_COST_MULTIPLIER")
    elite_portfolio_builder_enabled: bool = Field(default=True, validation_alias="ELITE_PORTFOLIO_BUILDER_ENABLED")
    elite_portfolio_activation_enabled: bool = Field(default=False, validation_alias="ELITE_PORTFOLIO_ACTIVATION_ENABLED")
    model_risk_enabled: bool = Field(default=True, validation_alias="MODEL_RISK_ENABLED")
    model_risk_authority: str = Field(default="shadow", validation_alias="MODEL_RISK_AUTHORITY")
    model_risk_max_risk_pct: float = Field(default=0.005, validation_alias="MODEL_RISK_MAX_RISK_PCT")
    model_risk_min_confidence: float = Field(default=0.65, validation_alias="MODEL_RISK_MIN_CONFIDENCE")
    portfolio_correlation_limit: float = Field(default=0.80, validation_alias="KEFTRADE_PORTFOLIO_CORRELATION_LIMIT")
    broker_allocated_capital: float = Field(default=10000, validation_alias="KEFTRADE_BROKER_ALLOCATED_CAPITAL")
    max_broker_risk_per_trade_pct: float = Field(default=0.01, validation_alias="KEFTRADE_MAX_RISK_PER_TRADE_PCT")
    max_broker_total_exposure_pct: float = Field(default=0.03, validation_alias="KEFTRADE_MAX_TOTAL_EXPOSURE_PCT")
    broker_daily_loss_limit_pct: float = Field(default=0.02, validation_alias="KEFTRADE_DAILY_LOSS_LIMIT_PCT")
    broker_weekly_loss_limit_pct: float = Field(default=0.05, validation_alias="KEFTRADE_WEEKLY_LOSS_LIMIT_PCT")
    broker_max_open_positions: int = Field(default=2, validation_alias="KEFTRADE_MAX_OPEN_POSITIONS")
    broker_max_open_orders: int = Field(default=2, validation_alias="KEFTRADE_MAX_OPEN_ORDERS")
    broker_worker_poll_seconds: int = Field(default=60, validation_alias="BROKER_WORKER_POLL_SECONDS")
    paper_scan_max_candle_age_hours: int = 96
    max_campaign_workers: int | None = Field(default=None, validation_alias="KEFTRADE_MAX_CAMPAIGN_WORKERS")
    campaign_worker_heartbeat_seconds: int = Field(default=10, validation_alias="KEFTRADE_CAMPAIGN_WORKER_HEARTBEAT_SECONDS")
    campaign_worker_stale_seconds: int = Field(default=45, validation_alias="KEFTRADE_CAMPAIGN_WORKER_STALE_SECONDS")
    campaign_worker_nice: int = Field(default=5, validation_alias="KEFTRADE_CAMPAIGN_WORKER_NICE")
    campaign_backtest_candle_limit: int = Field(default=4000, validation_alias="KEFTRADE_CAMPAIGN_BACKTEST_CANDLE_LIMIT")
    campaign_dataset_cache_entries: int = Field(default=8, validation_alias="KEFTRADE_CAMPAIGN_DATASET_CACHE_ENTRIES")

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()


def cors_origin_list() -> list[str]:
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
