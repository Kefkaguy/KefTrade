from app.domain.assets import ALPACA_IEX_PROVIDER, DEFAULT_DEV_PROVIDER, US_EQUITY_RESEARCH_UNIVERSE, US_EQUITY_VALIDATION_UNIVERSE, YFINANCE_RESEARCH_PROVIDER
from app.domain.market_data import MarketDataSyncResult
from app.providers.registry import get_market_data_provider
from app.providers.yfinance_provider import STATIC_STOCK_METADATA
from datetime import UTC, datetime
import asyncio


def test_dev_market_data_provider_is_registered() -> None:
    provider = get_market_data_provider(DEFAULT_DEV_PROVIDER)

    assert provider.name == DEFAULT_DEV_PROVIDER
    assert hasattr(provider, "sync_candles")


def test_stock_research_universe_is_explicit() -> None:
    assert US_EQUITY_RESEARCH_UNIVERSE == (
        "AAPL",
        "MSFT",
        "NVDA",
        "AMD",
        "META",
        "AMZN",
        "GOOGL",
        "TSLA",
        "SPY",
        "QQQ",
    )


def test_fallback_provider_covers_every_stock_research_asset() -> None:
    assert set(US_EQUITY_RESEARCH_UNIVERSE).issubset(STATIC_STOCK_METADATA)


def test_stock_validation_universe_is_explicit() -> None:
    assert US_EQUITY_VALIDATION_UNIVERSE == ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA")


def test_yfinance_research_provider_is_registered() -> None:
    provider = get_market_data_provider(YFINANCE_RESEARCH_PROVIDER)

    assert provider.name == YFINANCE_RESEARCH_PROVIDER
    assert hasattr(provider, "sync_candles")


def test_alpaca_iex_provider_is_registered() -> None:
    provider = get_market_data_provider(ALPACA_IEX_PROVIDER)

    assert provider.name == ALPACA_IEX_PROVIDER
    assert hasattr(provider, "sync_candles")


def test_data_sync_routes_through_selected_provider(monkeypatch) -> None:
    from app.routers import data

    calls = []

    class FakeProvider:
        name = "fake_provider"

        async def sync_candles(self, conn, symbol: str, timeframe: str, limit: int) -> MarketDataSyncResult:
            calls.append({"conn": conn, "symbol": symbol, "timeframe": timeframe, "limit": limit})
            timestamp = datetime(2024, 1, 1, tzinfo=UTC)
            return MarketDataSyncResult(
                symbol=symbol,
                timeframe=timeframe,
                provider=self.name,
                received=1,
                upserted=1,
                candle_count=1,
                missing_intervals=0,
                duplicate_count=0,
                incomplete_latest_candle_excluded=False,
                first_timestamp=timestamp,
                last_timestamp=timestamp,
            )

    monkeypatch.setattr(data, "get_market_data_provider", lambda provider: FakeProvider())

    result = asyncio.run(data.sync_data(symbol="AAPL", timeframe="1d", provider="fake_provider", limit=10, conn="db"))

    assert result["provider"] == "fake_provider"
    assert calls == [{"conn": "db", "symbol": "AAPL", "timeframe": "1d", "limit": 10}]
