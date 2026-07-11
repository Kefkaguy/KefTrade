from app.providers.alpaca import AlpacaMarketDataProvider
from app.providers.binance import BinanceMarketDataProvider
from app.providers.yfinance_provider import YFinanceMarketDataProvider

market_data_providers = {
    "alpaca_iex": AlpacaMarketDataProvider(),
    "binance_dev": BinanceMarketDataProvider(),
    "yfinance_research": YFinanceMarketDataProvider(),
}


def get_market_data_provider(name: str):
    try:
        return market_data_providers[name]
    except KeyError as exc:
        supported = ", ".join(sorted(market_data_providers))
        raise ValueError(f"Unsupported market data provider '{name}'. Supported providers: {supported}") from exc
