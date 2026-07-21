from decimal import Decimal

from app.services.strategy import StrategyDecision
from app.services.strategy_diagnostics import enrich_decision


def test_diagnostics_report_all_independent_failures() -> None:
    decision = StrategyDecision("avoid", None, None, None, None, ["Trend filter failed."])
    candle = {"symbol": "AAXJ", "timeframe": "4h", "close": Decimal("90")}
    feature = {"ema_20": Decimal("95"), "ema_50": Decimal("100"), "rsi_14": Decimal("38"), "volume_change": Decimal("-0.40")}
    params = {"ema_fast": 20, "ema_slow": 50, "rsi_min": 45, "rsi_max": 65, "volume_change_min": Decimal("-0.25"), "entry_distance_to_ema20_max": Decimal("0.035")}

    enriched = enrich_decision(decision, candle, feature, [], params)
    failed = {gate["code"] for gate in enriched.gates if gate["status"] == "failed"}

    assert {"TREND_PRICE_ABOVE_SLOW", "TREND_FAST_ABOVE_SLOW", "MOMENTUM_RSI_RANGE", "VOLUME_CHANGE_MIN", "ENTRY_DISTANCE_MAX"} <= failed
    assert any(gate["status"] == "not_evaluated" and gate["code"] == "STOP_BELOW_ENTRY" for gate in enriched.gates)


def test_diagnostics_do_not_change_frozen_signal() -> None:
    decision = StrategyDecision("watchlist", None, None, None, None, ["Wait for confirmation."])
    enriched = enrich_decision(
        decision,
        {"symbol": "AAPL", "timeframe": "1h", "close": Decimal("110")},
        {"ema_20": Decimal("105"), "ema_50": Decimal("100"), "rsi_14": Decimal("50")},
        [],
        {"ema_fast": 20, "ema_slow": 50, "rsi_min": 40, "rsi_max": 60},
    )
    assert enriched.signal == "watchlist"
    assert enriched.decision_version == "structured-gates-v1"
