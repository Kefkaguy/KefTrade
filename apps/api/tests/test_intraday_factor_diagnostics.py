from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.intraday_factor_diagnostics import (
    evaluate_factor_discovery,
    first_to_last_half_hour_observations,
)


def market_candles(symbol: str, sessions: int = 120):
    rows = []
    previous_close = 100.0
    start = date(2025, 1, 2)
    for session_index in range(sessions):
        day = start + timedelta(days=session_index)
        direction = 1 if session_index % 2 == 0 else -1
        opening_return = direction * (0.006 + (session_index % 3) * 0.0002)
        first_close = previous_close * (1 + opening_return)
        for bar_index in range(13):
            timestamp = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC) + timedelta(minutes=30 * bar_index)
            open_price = previous_close if bar_index == 0 else first_close
            target = direction * (0.005 + (session_index % 5) * 0.0001) if bar_index == 12 else 0
            close = open_price * (1 + target) if bar_index == 12 else first_close
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "open": Decimal(str(open_price)),
                    "high": Decimal(str(max(open_price, close) * 1.001)),
                    "low": Decimal(str(min(open_price, close) * 0.999)),
                    "close": Decimal(str(close)),
                    "volume": Decimal("100000"),
                }
            )
        previous_close = float(rows[-1]["close"])
    return rows


def test_first_to_last_observation_uses_opening_signal_and_last_bar_target():
    candles = {"SPY": market_candles("SPY", sessions=12)}

    observations = first_to_last_half_hour_observations(candles, timeframe="30m")

    assert len(observations) == 11
    assert observations[0]["score"] < 0
    assert observations[0]["target_return"] < 0


def test_discovery_never_calculates_withheld_confirmation_metrics():
    candles = {
        "SPY": market_candles("SPY"),
        "QQQ": market_candles("QQQ"),
    }
    cost_model = {
        "observed_round_trip_bps": 5,
        "stressed_round_trip_bps": 20,
        "conservative_round_trip_bps": 30,
    }

    result = evaluate_factor_discovery(
        candles,
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum", "liquidity_shock_reversal"],
        cost_model=cost_model,
    )

    market = result["factors"]["first_to_last_half_hour_market_momentum"]
    assert result["confirmation_data_accessed"] is False
    assert market["confirmation"]["status"] == "locked"
    assert "confirmation" not in market["confirmation"] or "rank_ic" not in market["confirmation"]
    assert market["validation"]["observations"] >= 50
    assert market["cost_clearance"]["clears_stressed"] is True
    assert result["factors"]["liquidity_shock_reversal"]["status"] == "blocked_missing_quote_data"
