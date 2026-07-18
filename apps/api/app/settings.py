from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    environment: str = "development"
    log_level: str = "INFO"
    diagnostic_logging: bool = True
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
