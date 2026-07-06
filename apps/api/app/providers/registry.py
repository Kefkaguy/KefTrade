from app.providers.binance import BinanceMarketDataProvider

market_data_providers = {
    "binance_dev": BinanceMarketDataProvider(),
}


def get_market_data_provider(name: str):
    try:
        return market_data_providers[name]
    except KeyError as exc:
        supported = ", ".join(sorted(market_data_providers))
        raise ValueError(f"Unsupported market data provider '{name}'. Supported providers: {supported}") from exc

