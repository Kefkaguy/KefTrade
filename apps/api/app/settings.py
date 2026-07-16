from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str = "postgresql://keftrade:keftrade@127.0.0.1:5432/keftrade"
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

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
