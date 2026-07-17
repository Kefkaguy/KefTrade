from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.deeper_observations import (
    OBSERVATION_DEFINITIONS,
    PHASE_5_OBSERVATION_VERSION,
    aggregate_deeper_observations,
    build_phase5_hypotheses,
    calculate_deeper_observation_series,
)
from app.services.research_architecture import calculate_asset_profile, generate_targeted_candidates


def candles(count: int = 180, *, symbol: str = "AAPL") -> list[dict]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    previous = 100.0
    for index in range(count):
        drift = index * 0.08
        wave = (index % 12) * 0.03
        close = 100 + drift + wave
        open_price = previous
        high = max(open_price, close) + 0.35
        low = min(open_price, close) - 0.35
        volume = 1000 + (index % 20) * 25
        if index % 45 == 0:
            volume *= 2
            high += 0.8
            close = high - 0.1
        rows.append(
            {
                "symbol": symbol,
                "timeframe": "1h",
                "timestamp": start + timedelta(hours=index),
                "open": Decimal(str(round(open_price, 6))),
                "high": Decimal(str(round(high, 6))),
                "low": Decimal(str(round(low, 6))),
                "close": Decimal(str(round(close, 6))),
                "volume": Decimal(str(round(volume, 6))),
            }
        )
        previous = close
    return rows


def test_deeper_observations_cover_required_market_structure_definitions() -> None:
    bundle = aggregate_deeper_observations(candles())

    required = {
        "trend_maturity",
        "trend_acceleration",
        "volatility_contraction",
        "volatility_expansion",
        "breakout_quality",
        "pullback_quality",
        "momentum_persistence",
        "exhaustion",
        "liquidity_expansion",
        "false_breakout",
        "structural_shift",
    }

    assert set(bundle["observations"]) == required
    assert bundle["calculation_version"] == PHASE_5_OBSERVATION_VERSION
    assert bundle["leakage_controls"]["uses_future_bars"] is False
    for key, row in bundle["observations"].items():
        assert key in bundle["definitions"]
        assert 0 <= row["score"] <= 1
        assert 0 <= row["event_rate"] <= 1
        assert row["expected_range"] == "0.0 to 1.0"
        assert row["sample_size"] > 0


def test_observations_do_not_change_when_future_candles_change() -> None:
    original = candles(150)
    changed_future = [dict(row) for row in original]
    for row in changed_future[120:]:
        row["close"] = Decimal(str(float(row["close"]) * 1.5))
        row["high"] = Decimal(str(float(row["high"]) * 1.5))
        row["low"] = Decimal(str(float(row["low"]) * 1.5))

    original_series = calculate_deeper_observation_series(original)
    changed_series = calculate_deeper_observation_series(changed_future)

    original_prefix = [row for row in original_series if row["timestamp"] < original[120]["timestamp"].isoformat()]
    changed_prefix = [row for row in changed_series if row["timestamp"] < original[120]["timestamp"].isoformat()]
    assert original_prefix == changed_prefix


def test_phase5_hypotheses_are_post_hoc_unconfirmed_and_generator_consumable() -> None:
    market_bundle = aggregate_deeper_observations(candles())
    observations = {
        "markets": [{"symbol": "AAPL", "timeframe": "1h", "series_sample_size": market_bundle["series_sample_size"], "observations": market_bundle["observations"]}],
        "observations": {key: {**row, "strategy_family": next(item.strategy_family for item in OBSERVATION_DEFINITIONS if item.key == key)} for key, row in market_bundle["observations"].items()},
        "market_count": 1,
        "calculation_version": PHASE_5_OBSERVATION_VERSION,
    }
    manifest = {"id": 4, "dataset_key": "dataset_test", "assets": ["AAPL"], "timeframes": ["1h"]}

    hypotheses = build_phase5_hypotheses(manifest, observations, max_hypotheses=5)

    assert len(hypotheses) == 5
    for index, hypothesis in enumerate(hypotheses, start=1):
        hypothesis["id"] = index
        assert hypothesis["status"] if "status" in hypothesis else "proposed"
        assert hypothesis["creation_source"] == "phase5_deeper_observations"
        assert hypothesis["test_summary"]["post_hoc"] is True
        assert hypothesis["test_summary"]["confirmation_status"] == "unconfirmed"
        assert hypothesis["test_summary"]["candidate_generation_contract"].startswith("standard generate_targeted_candidates")
        generated = generate_targeted_candidates(hypothesis, max_candidates=3)
        assert len(generated["candidates"]) == 3
        assert all(candidate.parameters["hypothesis_version_id"] == index for candidate in generated["candidates"])


def test_deeper_observations_are_available_in_asset_profile_observation_layer() -> None:
    profile = calculate_asset_profile(candles())

    observations = profile["metrics"]["market_structure_observations"]

    assert set(observations) == {item.key for item in OBSERVATION_DEFINITIONS}
    assert profile["metrics"]["trend_maturity_score"] == observations["trend_maturity"]["score"]
    assert profile["metrics"]["volatility_contraction_event_rate"] == observations["volatility_contraction"]["event_rate"]
