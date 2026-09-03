from app.providers import alpaca
from app.services import research_campaigns


def test_alpaca_supports_direct_short_intraday_bars() -> None:
    assert {timeframe: alpaca.SUPPORTED_TIMEFRAMES[timeframe] for timeframe in ("1m", "3m", "5m", "15m", "30m")} == {
        "1m": "1Min",
        "3m": "3Min",
        "5m": "5Min",
        "15m": "15Min",
        "30m": "30Min",
    }
    assert {timeframe: alpaca.TIMEFRAME_SECONDS[timeframe] for timeframe in ("1m", "3m", "5m")} == {
        "1m": 60,
        "3m": 180,
        "5m": 300,
    }


def test_campaign_path_uses_meaningful_short_timeframe_depths() -> None:
    requested = {"1m", "3m", "5m", "15m", "30m"}
    assert requested.issubset(set(research_campaigns.SUPPORTED_CAMPAIGN_TIMEFRAMES))
    assert requested == set(research_campaigns.HIGH_FREQUENCY_TIMEFRAMES)
    assert research_campaigns.minimum_campaign_candles("1m") == 4000
    assert research_campaigns.minimum_campaign_candles("3m") == 4000
    assert research_campaigns.minimum_campaign_candles("5m") == 4000
    assert research_campaigns.minimum_campaign_candles("15m") == 120
    assert research_campaigns.campaign_candle_limit("1m") >= 50_000
    assert research_campaigns.campaign_candle_limit("3m") >= 30_000
    assert research_campaigns.campaign_candle_limit("5m") >= 20_000
