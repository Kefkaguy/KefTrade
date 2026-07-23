from __future__ import annotations

from app.services.family_registry import ACTIVE_CLASSIFICATIONS, classify_family


def stats(**overrides):
    base = {"median_profit_factor": 1.0, "avg_trades": 20.0, "elites": 0}
    base.update(overrides)
    return base


def test_dead_family_is_retired() -> None:
    classification, _ = classify_family(stats(median_profit_factor=None, avg_trades=0))
    assert classification == "Retire: dead (never trades)"
    assert classification not in ACTIVE_CLASSIFICATIONS


def test_elite_family_with_robust_median_is_excellent_and_active() -> None:
    classification, _ = classify_family(stats(median_profit_factor=1.47, avg_trades=39, elites=1))
    assert classification == "Excellent"
    assert classification in ACTIVE_CLASSIFICATIONS


def test_high_pf_low_trades_is_too_restrictive_and_stays_active() -> None:
    classification, _ = classify_family(stats(median_profit_factor=5.8, avg_trades=9))
    assert classification == "Too restrictive"
    assert classification in ACTIVE_CLASSIFICATIONS


def test_promising_family_stays_active() -> None:
    classification, _ = classify_family(stats(median_profit_factor=1.6, avg_trades=25))
    assert classification == "Good: promising, under-promoted"
    assert classification in ACTIVE_CLASSIFICATIONS


def test_negative_edge_and_noisy_families_are_legacy() -> None:
    for overrides in (dict(median_profit_factor=0.8, avg_trades=10), dict(median_profit_factor=0.9, avg_trades=40), dict(median_profit_factor=1.1, avg_trades=20)):
        classification, _ = classify_family(stats(**overrides))
        assert classification not in ACTIVE_CLASSIFICATIONS


def test_classification_is_deterministic() -> None:
    values = stats(median_profit_factor=1.23, avg_trades=17)
    assert classify_family(values) == classify_family(values)


def test_low_timeframes_are_supported_but_not_default() -> None:
    """15m/30m must be usable when asked for, without changing defaults."""
    from app.services.research_campaigns import (
        DEFAULT_CAMPAIGN_TIMEFRAMES,
        HIGH_FREQUENCY_TIMEFRAMES,
        SUPPORTED_CAMPAIGN_TIMEFRAMES,
    )

    assert DEFAULT_CAMPAIGN_TIMEFRAMES == ("1h", "4h", "1d")
    for timeframe in ("15m", "30m"):
        assert timeframe in SUPPORTED_CAMPAIGN_TIMEFRAMES
        assert timeframe not in DEFAULT_CAMPAIGN_TIMEFRAMES
    assert set(DEFAULT_CAMPAIGN_TIMEFRAMES).issubset(set(SUPPORTED_CAMPAIGN_TIMEFRAMES))
    assert set(HIGH_FREQUENCY_TIMEFRAMES).issubset(set(SUPPORTED_CAMPAIGN_TIMEFRAMES))


def test_explicit_low_timeframes_are_no_longer_stripped() -> None:
    from app.services.research_campaigns import strongest_quality_timeframes

    assert strongest_quality_timeframes([], [], ["15m", "30m"]) == ["15m", "30m"]
    assert strongest_quality_timeframes([], [], ["15m", "bogus"]) == ["15m"]


def test_move_thresholds_scale_with_sqrt_of_bar_duration() -> None:
    from app.services.research_campaigns import timeframe_scaled_parameters

    authored = {"returns_5_min": 0.012, "entry_distance_to_ema20_max": 0.02, "risk_reward": 1.5, "rsi_max": 72, "fee_rate": 0.001}
    scaled = timeframe_scaled_parameters(authored, "15m")

    # 15m is 1/16th of 4h, so moves scale by sqrt(1/16) = 0.25
    assert scaled["timeframe_scaling_factor"] == 0.25
    assert scaled["returns_5_min"] == 0.003
    assert scaled["entry_distance_to_ema20_max"] == 0.005
    # Ratios, periods and costs are never rescaled
    assert scaled["risk_reward"] == 1.5
    assert scaled["rsi_max"] == 72
    assert scaled["fee_rate"] == 0.001


def test_reference_timeframe_is_unchanged_and_scaling_is_opt_in() -> None:
    from app.services.research_campaigns import DiscoveryCandidate, apply_timeframe_scaling, timeframe_scaled_parameters

    authored = {"returns_5_min": 0.012}
    assert timeframe_scaled_parameters(authored, "4h") == authored

    def make(params):
        return DiscoveryCandidate(
            candidate_id="c1", family_id="f1", parent_candidate_id=None, generation=1,
            blocks={"entry": "breakout"}, parameters=params, complexity=1, canonical_key="k1",
        )

    # Not opted in -> untouched, so existing evidence keeps its semantics.
    assert apply_timeframe_scaling(make(dict(authored)), "15m").parameters == authored
    opted = apply_timeframe_scaling(make({**authored, "timeframe_scaled_thresholds": True}), "15m")
    assert opted.parameters["returns_5_min"] == 0.003


def test_campaign_variant_prevents_key_collision() -> None:
    """A different generator variant must not reuse an earlier campaign's row."""
    from app.services.research_campaigns import research_campaign_key

    base = research_campaign_key("u", ["AAPL"], ["15m"], 69, search_mode="high_frequency")
    scaled = research_campaign_key("u", ["AAPL"], ["15m"], 69, search_mode="high_frequency", variant="timeframe_scaled_v2")
    assert base != scaled
    # Same inputs still deduplicate as before.
    assert scaled == research_campaign_key("u", ["AAPL"], ["15m"], 69, search_mode="high_frequency", variant="timeframe_scaled_v2")
