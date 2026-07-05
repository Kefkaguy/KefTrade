from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://keftrade:keftrade@127.0.0.1:5432/keftrade"
    binance_base_url: str = "https://api.binance.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
