from dataclasses import dataclass
from typing import Literal

AssetClass = Literal["crypto", "us_equity", "etf"]


@dataclass(frozen=True)
class ResearchAsset:
    symbol: str
    asset_class: AssetClass
    exchange: str
    currency: str
    name: str
    provider_symbol: str
    sector: str | None = None
    market_cap: int | None = None
    index_membership: tuple[str, ...] = ()


BTC_DEV_ASSET = ResearchAsset(
    symbol="BTCUSDT",
    asset_class="crypto",
    exchange="BINANCE",
    currency="USDT",
    name="Bitcoin / Tether USD",
    provider_symbol="BTCUSDT",
)

US_EQUITY_RESEARCH_UNIVERSE = (
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

DEFAULT_DEV_SYMBOL = BTC_DEV_ASSET.symbol
DEFAULT_DEV_TIMEFRAME = "4h"
DEFAULT_DEV_PROVIDER = "binance_dev"

