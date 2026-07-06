from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.providers.binance import count_duplicate_raw_klines, detect_missing_intervals, exclude_incomplete_latest


def test_missing_candle_detection_counts_four_hour_gaps() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [
        {"timestamp": start, "open": Decimal("1")},
        {"timestamp": start + timedelta(hours=4), "open": Decimal("1")},
        {"timestamp": start + timedelta(hours=12), "open": Decimal("1")},
    ]

    assert detect_missing_intervals(candles, "4h") == 1


def test_incomplete_latest_candle_is_excluded() -> None:
    complete = [1704067200000, "1", "2", "1", "2", "10", 1704081599999]
    incomplete = [1704081600000, "2", "3", "2", "3", "10", 1704095999999]

    rows, excluded = exclude_incomplete_latest([complete, incomplete], now_ms=1704085200000)

    assert excluded is True
    assert rows == [complete]


def test_duplicate_raw_klines_are_counted_before_dedupe() -> None:
    first = [1704067200000, "1", "2", "1", "2", "10", 1704081599999]
    duplicate = [1704067200000, "1", "2", "1", "2", "10", 1704081599999]
    second = [1704081600000, "2", "3", "2", "3", "10", 1704095999999]

    assert count_duplicate_raw_klines([first, duplicate, second]) == 1
