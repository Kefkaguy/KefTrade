from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from app.providers.yfinance_provider import (
    detect_missing_trading_sessions,
    exclude_incomplete_latest_daily,
    normalize_history,
    valid_ohlc,
)


def test_valid_ohlc_rejects_invalid_stock_candles() -> None:
    assert valid_ohlc(Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"), Decimal("100")) is True
    assert valid_ohlc(Decimal("12"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal("100")) is False
    assert valid_ohlc(Decimal("10"), Decimal("11"), Decimal("9"), Decimal("12"), Decimal("100")) is False
    assert valid_ohlc(Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal("-1")) is False


def test_normalize_history_counts_invalid_ohlc_rows() -> None:
    history = pd.DataFrame(
        [
            {"Open": 10, "High": 11, "Low": 9, "Close": 10.5, "Volume": 100},
            {"Open": 12, "High": 11, "Low": 9, "Close": 10.5, "Volume": 100},
        ],
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )

    candles, invalid_count = normalize_history("AAPL", "1d", history)

    assert len(candles) == 1
    assert invalid_count == 1
    assert candles[0]["symbol"] == "AAPL"


def test_missing_trading_sessions_ignores_weekends() -> None:
    start = datetime(2024, 1, 5, 5, 0, tzinfo=UTC)  # Friday in US/Eastern
    candles = [
        {"timestamp": start},
        {"timestamp": start + timedelta(days=3)},  # Monday, no missing weekday
        {"timestamp": start + timedelta(days=5)},  # Wednesday, Tuesday is missing
    ]

    assert detect_missing_trading_sessions(candles) == 1


def test_missing_trading_sessions_ignores_us_equity_holidays() -> None:
    candles = [
        {"timestamp": datetime(2024, 7, 3, 4, 0, tzinfo=UTC)},
        {"timestamp": datetime(2024, 7, 5, 4, 0, tzinfo=UTC)},
    ]

    assert detect_missing_trading_sessions(candles) == 0


def test_missing_trading_sessions_ignores_thanksgiving_and_special_closure() -> None:
    thanksgiving = [
        {"timestamp": datetime(2024, 11, 27, 5, 0, tzinfo=UTC)},
        {"timestamp": datetime(2024, 11, 29, 5, 0, tzinfo=UTC)},
    ]
    special_closure = [
        {"timestamp": datetime(2025, 1, 8, 5, 0, tzinfo=UTC)},
        {"timestamp": datetime(2025, 1, 10, 5, 0, tzinfo=UTC)},
    ]

    assert detect_missing_trading_sessions(thanksgiving) == 0
    assert detect_missing_trading_sessions(special_closure) == 0


def test_incomplete_latest_daily_candle_is_excluded_before_market_close() -> None:
    today = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)  # 09:30 ET
    candles = [
        {"timestamp": datetime(2026, 7, 3, 4, 0, tzinfo=UTC)},
        {"timestamp": datetime(2026, 7, 6, 4, 0, tzinfo=UTC)},
    ]

    rows, excluded = exclude_incomplete_latest_daily(candles, now=today)

    assert excluded is True
    assert rows == candles[:1]
