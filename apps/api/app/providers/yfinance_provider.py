from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    DateOffset,
    Easter,
    Holiday,
    MO,
    TH,
    nearest_workday,
)
import psycopg
from psycopg.types.json import Jsonb

from app.domain.market_data import MarketDataSyncResult

YFINANCE_SOURCE = "yfinance"
YFINANCE_ENDPOINT = "Ticker.history"
SUPPORTED_TIMEFRAMES = {"1d"}
MARKET_CLOSE = time(16, 0)
EASTERN = ZoneInfo("America/New_York")


class UsEquityHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Years Day", month=1, day=1, observance=nearest_workday),
        Holiday("Martin Luther King Jr Day", month=1, day=1, offset=DateOffset(weekday=MO(3))),
        Holiday("Washingtons Birthday", month=2, day=1, offset=DateOffset(weekday=MO(3))),
        Holiday("Good Friday", month=1, day=1, offset=[Easter(), DateOffset(days=-2)]),
        Holiday("Memorial Day", month=5, day=31, offset=DateOffset(weekday=MO(-1))),
        Holiday("Juneteenth", month=6, day=19, start_date="2022-06-19", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        Holiday("Labor Day", month=9, day=1, offset=DateOffset(weekday=MO(1))),
        Holiday("Thanksgiving Day", month=11, day=1, offset=DateOffset(weekday=TH(4))),
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
        Holiday("National Day of Mourning 2025", year=2025, month=1, day=9),
    ]


US_EQUITY_HOLIDAYS = UsEquityHolidayCalendar()

STATIC_STOCK_METADATA = {
    "SPY": {"name": "SPDR S&P 500 ETF Trust", "asset_class": "etf", "exchange": "NYSEARCA", "currency": "USD", "sector": None, "index_membership": ["S&P 500"]},
    "QQQ": {"name": "Invesco QQQ Trust", "asset_class": "etf", "exchange": "NASDAQ", "currency": "USD", "sector": None, "index_membership": ["NASDAQ 100"]},
    "AAPL": {"name": "Apple Inc.", "asset_class": "us_equity", "exchange": "NASDAQ", "currency": "USD", "sector": "Technology", "index_membership": ["S&P 500", "NASDAQ 100"]},
    "MSFT": {"name": "Microsoft Corporation", "asset_class": "us_equity", "exchange": "NASDAQ", "currency": "USD", "sector": "Technology", "index_membership": ["S&P 500", "NASDAQ 100"]},
    "NVDA": {"name": "NVIDIA Corporation", "asset_class": "us_equity", "exchange": "NASDAQ", "currency": "USD", "sector": "Technology", "index_membership": ["S&P 500", "NASDAQ 100"]},
    "TSLA": {"name": "Tesla, Inc.", "asset_class": "us_equity", "exchange": "NASDAQ", "currency": "USD", "sector": "Consumer Cyclical", "index_membership": ["S&P 500", "NASDAQ 100"]},
}


class YFinanceMarketDataProvider:
    name = "yfinance_research"

    async def sync_candles(self, conn: psycopg.Connection, symbol: str, timeframe: str, limit: int) -> MarketDataSyncResult:
        return sync_yfinance_candles(conn, symbol=symbol, timeframe=timeframe, limit=limit)


def sync_yfinance_candles(conn: psycopg.Connection, symbol: str, timeframe: str = "1d", limit: int = 1500) -> MarketDataSyncResult:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("yfinance_research supports 1d candles only.")
    if symbol not in STATIC_STOCK_METADATA:
        supported = ", ".join(sorted(STATIC_STOCK_METADATA))
        raise ValueError(f"Unsupported yfinance research symbol '{symbol}'. Supported symbols: {supported}")

    ensure_stock_symbol(conn, symbol)
    raw = fetch_yfinance_history(symbol, limit)
    duplicate_count = count_duplicate_timestamps(raw)
    candles, invalid_ohlc_count = normalize_history(symbol, timeframe, raw)
    candles, incomplete_excluded = exclude_incomplete_latest_daily(candles)
    missing_sessions = detect_missing_trading_sessions(candles)
    log_yfinance_response(conn, symbol, timeframe, limit, raw)
    upserted = upsert_candles(conn, candles)
    conn.commit()
    return MarketDataSyncResult(
        symbol=symbol,
        timeframe=timeframe,
        provider="yfinance_research",
        received=len(candles),
        upserted=upserted,
        candle_count=len(candles),
        missing_intervals=missing_sessions,
        duplicate_count=duplicate_count,
        incomplete_latest_candle_excluded=incomplete_excluded,
        first_timestamp=candles[0]["timestamp"] if candles else None,
        last_timestamp=candles[-1]["timestamp"] if candles else None,
        invalid_ohlc_count=invalid_ohlc_count,
    )


def fetch_yfinance_history(symbol: str, limit: int) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Install yfinance to use the yfinance_research provider.") from exc

    period = "max" if limit > 1000 else "5y"
    ticker = yf.Ticker(symbol)
    history = ticker.history(period=period, interval="1d", auto_adjust=False, actions=False)
    if history.empty:
        return history
    return history.tail(limit)


def normalize_history(symbol: str, timeframe: str, history: pd.DataFrame) -> tuple[list[dict[str, Any]], int]:
    candles = []
    invalid = 0
    if history.empty:
        return candles, invalid

    for timestamp, row in history.iterrows():
        candle = normalize_history_row(symbol, timeframe, timestamp, row)
        if candle is None:
            invalid += 1
            continue
        candles.append(candle)
    return candles, invalid


def normalize_history_row(symbol: str, timeframe: str, timestamp: Any, row: Any) -> dict[str, Any] | None:
    try:
        open_price = Decimal(str(row["Open"]))
        high = Decimal(str(row["High"]))
        low = Decimal(str(row["Low"]))
        close = Decimal(str(row["Close"]))
        volume = Decimal(str(row["Volume"]))
    except (InvalidOperation, KeyError, ValueError):
        return None

    if not valid_ohlc(open_price, high, low, close, volume):
        return None

    parsed_timestamp = pd.Timestamp(timestamp)
    if parsed_timestamp.tzinfo is None:
        parsed_timestamp = parsed_timestamp.tz_localize(EASTERN)
    return {
        "symbol": symbol,
        "source": YFINANCE_SOURCE,
        "timeframe": timeframe,
        "timestamp": parsed_timestamp.tz_convert(UTC).to_pydatetime(),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def valid_ohlc(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal) -> bool:
    return (
        open_price > 0
        and high > 0
        and low > 0
        and close > 0
        and volume >= 0
        and high >= low
        and low <= open_price <= high
        and low <= close <= high
    )


def exclude_incomplete_latest_daily(candles: list[dict[str, Any]], now: datetime | None = None) -> tuple[list[dict[str, Any]], bool]:
    if not candles:
        return candles, False
    now = now or datetime.now(tz=UTC)
    latest_local = candles[-1]["timestamp"].astimezone(EASTERN).date()
    now_local = now.astimezone(EASTERN)
    if latest_local == now_local.date() and now_local.time() < MARKET_CLOSE:
        return candles[:-1], True
    return candles, False


def count_duplicate_timestamps(history: pd.DataFrame) -> int:
    if history.empty:
        return 0
    return int(history.index.duplicated().sum())


def detect_missing_trading_sessions(candles: list[dict[str, Any]]) -> int:
    if len(candles) < 2:
        return 0
    ordered_dates = [candle["timestamp"].astimezone(EASTERN).date() for candle in sorted(candles, key=lambda row: row["timestamp"])]
    observed = set(ordered_dates)
    holidays = {
        holiday.date()
        for holiday in US_EQUITY_HOLIDAYS.holidays(
            start=pd.Timestamp(ordered_dates[0]),
            end=pd.Timestamp(ordered_dates[-1]),
        )
    }
    missing = 0
    current = ordered_dates[0]
    end = ordered_dates[-1]
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in holidays and current not in observed:
            missing += 1
    return missing


def ensure_stock_symbol(conn: psycopg.Connection, symbol: str) -> None:
    metadata = STATIC_STOCK_METADATA[symbol]
    conn.execute(
        """
        INSERT INTO symbols(symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, sector, index_membership)
        VALUES (%s, %s, %s, %s, %s, %s, 'yfinance_research', %s, %s)
        ON CONFLICT (symbol)
        DO UPDATE SET
            asset_class = EXCLUDED.asset_class,
            exchange = EXCLUDED.exchange,
            currency = EXCLUDED.currency,
            name = EXCLUDED.name,
            provider_symbol = EXCLUDED.provider_symbol,
            primary_provider = EXCLUDED.primary_provider,
            sector = EXCLUDED.sector,
            index_membership = EXCLUDED.index_membership
        """,
        (
            symbol,
            metadata["asset_class"],
            metadata["exchange"],
            metadata["currency"],
            metadata["name"],
            symbol,
            metadata["sector"],
            Jsonb(metadata["index_membership"]),
        ),
    )


def log_yfinance_response(conn: psycopg.Connection, symbol: str, timeframe: str, limit: int, history: pd.DataFrame) -> None:
    body = {
        "row_count": int(len(history)),
        "first_timestamp": str(history.index[0]) if not history.empty else None,
        "last_timestamp": str(history.index[-1]) if not history.empty else None,
        "columns": list(history.columns),
    }
    conn.execute(
        """
        INSERT INTO raw_api_logs(source, endpoint, request_params, response_status, response_body)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (YFINANCE_SOURCE, YFINANCE_ENDPOINT, Jsonb({"symbol": symbol, "timeframe": timeframe, "limit": limit}), 200, Jsonb(body)),
    )


def upsert_candles(conn: psycopg.Connection, candles: list[dict[str, Any]]) -> int:
    affected = 0
    for candle in candles:
        result = conn.execute(
            """
            INSERT INTO candles(symbol, source, timeframe, timestamp, open, high, low, close, volume)
            VALUES (%(symbol)s, %(source)s, %(timeframe)s, %(timestamp)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
            ON CONFLICT(symbol, source, timeframe, timestamp)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
            """,
            candle,
        )
        affected += result.rowcount or 0
    return affected
