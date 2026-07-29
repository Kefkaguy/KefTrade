from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.labs.intraday.cross_sectional import compute_next_same_slot_percentiles


def candles(symbol: str, strength: float):
    rows = []
    start = date(2026, 1, 2)
    for session in range(10):
        day = start + timedelta(days=session)
        for slot in range(3):
            timestamp = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC) + timedelta(minutes=30 * slot)
            open_price = Decimal("100")
            close = Decimal(str(100 * (1 + strength * (slot + 1))))
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": timestamp,
                    "open": open_price,
                    "high": max(open_price, close),
                    "low": min(open_price, close),
                    "close": close,
                    "volume": Decimal("1000"),
                }
            )
    return rows


def test_next_same_slot_rank_is_keyed_to_signal_bar_before_target_opens():
    universe = {
        f"S{index}": candles(f"S{index}", (index - 2) * 0.001)
        for index in range(6)
    }

    ranks = compute_next_same_slot_percentiles(universe, lookback_sessions=20)

    sample_time = sorted(ranks["S5"])[0]
    assert sample_time.minute == 30
    assert ranks["S5"][sample_time]["percentile"] == 1
    assert ranks["S0"][sample_time]["percentile"] == 0
