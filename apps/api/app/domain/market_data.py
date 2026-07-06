from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class Candle:
    symbol: str
    provider: str
    timeframe: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class MarketDataSyncResult:
    symbol: str
    timeframe: str
    provider: str
    received: int
    upserted: int
    candle_count: int
    missing_intervals: int
    duplicate_count: int
    incomplete_latest_candle_excluded: bool
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    invalid_ohlc_count: int = 0


class MarketDataProvider(Protocol):
    name: str

    async def sync_candles(self, conn: Any, symbol: str, timeframe: str, limit: int) -> MarketDataSyncResult:
        ...


class TradingCalendar(Protocol):
    def is_session_open(self, timestamp: datetime, exchange: str) -> bool:
        ...

    def next_session_open(self, timestamp: datetime, exchange: str) -> datetime:
        ...


class CorporateActions(Protocol):
    def get_actions(self, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        ...


class SymbolMetadata(Protocol):
    def get_symbol(self, symbol: str) -> dict[str, Any] | None:
        ...


class ExchangeInfo(Protocol):
    def get_exchange(self, exchange: str) -> dict[str, Any] | None:
        ...
