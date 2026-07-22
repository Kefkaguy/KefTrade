from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.research_architecture import generate_phase2_family_hypotheses, generate_targeted_candidates
from app.services.strategy_discovery import candidate_execution_key, generate_family_discovery_candidates
from app.services.strategy_families import (
    PHASE_2_FAMILY_NAMES,
    PHASE_2_FAMILY_VERSION,
    STRATEGY_FAMILY_SPECS,
    family_specification_payload,
    strategy_family_decision,
)


def candle(index: int, open_price: float, high: float, low: float, close: float, volume: float = 1000) -> dict:
    return {
        "symbol": "TEST",
        "timeframe": "1h",
        "timestamp": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(hours=index),
        "open": Decimal(str(open_price)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": Decimal(str(volume)),
    }


def default_params(family: str) -> dict:
    spec = STRATEGY_FAMILY_SPECS[family]
    return {
        **{key: values[len(values) // 2] for key, values in spec.parameter_ranges.items()},
        "strategy_architecture": PHASE_2_FAMILY_VERSION,
        "phase2_strategy_family": family,
        "swing_lookback": 5,
    }


def standard_feature(**overrides) -> dict:
    return {
        "returns_1": Decimal("0.01"),
        "returns_5": Decimal("0.02"),
        "ema_20": Decimal("100"),
        "ema_50": Decimal("99"),
        "rsi_14": Decimal("60"),
        "macd": Decimal("1"),
        "macd_signal": Decimal("0.5"),
        "volume_change": Decimal("0.2"),
        "volatility_20": Decimal("0.01"),
        "distance_from_ema_20": Decimal("0.01"),
        "distance_from_ema_50": Decimal("0.02"),
        **overrides,
    }


def test_family_registry_is_complete_falsifiable_and_executable() -> None:
    assert PHASE_2_FAMILY_NAMES == (
        "Breakout",
        "Momentum",
        "Pullback",
        "Mean Reversion",
        "Volatility Expansion",
        "Range Breakout",
        "Continuation",
        "Gap",
        "Bearish Breakdown",
        "Bearish Momentum",
    )
    payload = family_specification_payload()
    assert payload["shared_controls"]["allocation"] == {
        "exploitation": 0.70,
        "nearby_controlled_mutation": 0.20,
        "broader_exploration": 0.10,
    }
    for spec in STRATEGY_FAMILY_SPECS.values():
        assert spec.observation_set
        assert spec.hypothesis_template
        assert spec.success_criteria
        assert spec.falsification_criteria
        assert spec.entry_logic != spec.confirmation_logic
        assert {"risk_reward", "atr_multiplier", "max_holding_bars"}.issubset(spec.parameter_ranges)
        assert spec.frequency_sensitive_parameters


def test_family_candidate_generation_is_deterministic_deduplicated_and_balanced() -> None:
    for family in PHASE_2_FAMILY_NAMES:
        first = generate_family_discovery_candidates(family, max_candidates=25, seed=17)
        second = generate_family_discovery_candidates(family, max_candidates=25, seed=17)
        assert [row.candidate_id for row in first] == [row.candidate_id for row in second]
        assert len({candidate_execution_key(row) for row in first}) == 25
        assert all(row.parameters["phase2_strategy_family"] == family for row in first)
        assert all(row.parameters["recent_candle_window_bars"] == 80 for row in first)
        hypothesis = {
            "id": 9,
            "hypothesis_key": f"test_{family}",
            "scope_type": "cluster",
            "scope_ref": "test_cluster",
            "strategy_family": family,
            "expected_behavior": "Measured behavior persists.",
            "relevant_regimes": [],
            "test_summary": {"generation_seed": 17},
        }
        generated = generate_targeted_candidates(hypothesis, max_candidates=20)
        assert generated["allocation"]["actual"] == {"exploitation": 14, "nearby": 4, "exploration": 2}
        assert len({candidate_execution_key(row) for row in generated["candidates"]}) == 20
        assert all(row.parent_candidate_id for row in generated["candidates"] if row.parameters["generation_channel"] == "nearby")


def test_each_family_has_a_distinct_executable_signal_path() -> None:
    flat = [candle(i, 100, 100.5, 99.5, 100, 1000) for i in range(60)]

    breakout = [candle(i, 100, 100.6, 99.4, 100, 1000) for i in range(54)]
    breakout.extend(candle(i, 100, 100.2, 99.8, 100, 1000) for i in range(54, 60))
    breakout.append(candle(60, 100.1, 101.0, 100.0, 100.9, 1500))

    momentum = []
    price = 100.0
    for i in range(61):
        next_price = price * (1.0015 if i < 50 else 1.004)
        momentum.append(candle(i, price, next_price + 0.08, price - 0.08, next_price, 1100))
        price = next_price

    pullback = []
    price = 100.0
    for i in range(66):
        next_price = price + 0.18
        pullback.append(candle(i, price, next_price + 0.15, price - 0.15, next_price, 1000))
        price = next_price
    for i, next_price in enumerate((111.2, 110.5, 109.6, 109.2, 110.2), start=66):
        pullback.append(candle(i, price, max(price, next_price) + 0.15, min(price, next_price) - 0.15, next_price, 1000))
        price = next_price

    mean_reversion = list(flat)
    mean_reversion[-2] = candle(58, 98, 98.2, 95.8, 96, 1000)
    mean_reversion[-1] = candle(59, 96, 97.4, 95.5, 97.2, 1100)

    expansion = list(flat[:40]) + [candle(40, 100, 103.2, 99.5, 103.0, 1500)]
    range_breakout = list(flat[:48]) + [candle(i, 100, 100.25, 99.75, 100, 1000) for i in range(48, 60)]
    range_breakout.append(candle(60, 100.1, 100.9, 100.0, 100.8, 1200))

    continuation = []
    price = 90.0
    for i in range(52):
        next_price = price + 0.16
        continuation.append(candle(i, price, next_price + 0.1, price - 0.1, next_price, 1000))
        price = next_price
    for i in range(52, 57):
        next_price = price + 0.35
        continuation.append(candle(i, price, next_price + 0.1, price - 0.1, next_price, 1100))
        price = next_price
    for i, next_price in enumerate((price - 0.1, price - 0.2, price - 0.08), start=57):
        continuation.append(candle(i, price, max(price, next_price) + 0.08, min(price, next_price) - 0.08, next_price, 900))
        price = next_price
    continuation.append(candle(60, price, price + 0.5, price - 0.05, price + 0.45, 1200))

    gap = list(flat[:60]) + [candle(60, 101, 101.8, 100.9, 101.7, 1300)]
    bearish_breakdown = [candle(i, 100, 100.6, 99.4, 100, 1000) for i in range(54)]
    bearish_breakdown.extend(candle(i, 100, 100.2, 99.8, 100, 1000) for i in range(54, 60))
    bearish_breakdown.append(candle(60, 99.9, 100.0, 98.8, 98.9, 1500))

    bearish_momentum = []
    price = 120.0
    for i in range(61):
        next_price = price * (0.9985 if i < 56 else 0.994)
        bearish_momentum.append(candle(i, price, price + 0.08, next_price - 0.08, next_price, 1100))
        price = next_price
    cases = {
        "Breakout": (breakout, standard_feature()),
        "Momentum": (momentum, standard_feature()),
        "Pullback": (pullback, standard_feature(rsi_14=Decimal("52"))),
        "Mean Reversion": (mean_reversion, standard_feature(ema_20=Decimal("100"), ema_50=Decimal("100"), rsi_14=Decimal("30"))),
        "Volatility Expansion": (expansion, standard_feature(volatility_20=Decimal("0.015"))),
        "Range Breakout": (range_breakout, standard_feature()),
        "Continuation": (continuation, standard_feature()),
        "Gap": (gap, standard_feature()),
        "Bearish Breakdown": (bearish_breakdown, standard_feature(rsi_14=Decimal("40"), macd=Decimal("-1"), macd_signal=Decimal("-0.5"))),
        "Bearish Momentum": (bearish_momentum, standard_feature(rsi_14=Decimal("40"), macd=Decimal("-1"), macd_signal=Decimal("-0.5"))),
    }
    explanations = set()
    for family, (rows, feature) in cases.items():
        decision = strategy_family_decision(rows[-1], feature, rows, default_params(family))
        assert decision.signal == "setup", (family, decision.explanation)
        assert decision.stop_loss is not None
        if decision.direction == "long":
            assert decision.stop_loss < rows[-1]["close"]
        else:
            assert decision.stop_loss > rows[-1]["close"]
            assert decision.take_profit is not None and decision.take_profit < rows[-1]["close"]
        explanations.add(decision.explanation[0])
    assert len(explanations) == len(PHASE_2_FAMILY_NAMES)


def test_family_decision_does_not_copy_or_mutate_recent_history() -> None:
    rows = [candle(i, 100, 101, 99, 100, 1000) for i in range(80)]
    original_ids = [id(row) for row in rows]

    strategy_family_decision(rows[-1], standard_feature(), rows, default_params("Breakout"))

    assert len(rows) == 80
    assert [id(row) for row in rows] == original_ids


def test_phase2_hypotheses_preserve_post_hoc_and_independent_confirmation_rules() -> None:
    profiles = []
    for profile_id, symbol in enumerate(("AAPL", "MSFT"), start=1):
        profiles.append(
            {
                "id": profile_id,
                "symbol": symbol,
                "timeframe": "1h",
                "metrics": {
                    "sample_size": 5000,
                    "realized_volatility": 0.012,
                    "atr_ratio": 0.014,
                    "atr_ratio_p90": 0.025,
                    "trend_persistence": 4.2,
                    "trend_strength": 0.04,
                    "return_autocorrelation_lag1": 0.03,
                    "mean_reversion_score": 0.02,
                    "reversal_rate": 0.5,
                    "breakout_follow_through": 0.58,
                    "median_pullback_depth": 0.02,
                    "momentum_persistence": 0.55,
                    "volume_expansion_ratio": 1.2,
                    "gap_frequency": 0.02,
                },
            }
        )
    clusters = [
        {
            "id": 7,
            "cluster_key": "cluster_test",
            "centroid": profiles[0]["metrics"],
            "members": [{"asset_profile_id": 1}, {"asset_profile_id": 2}],
            "quality_metrics": {"average_distance_to_centroid": 0.5},
        }
    ]
    hypotheses = generate_phase2_family_hypotheses(profiles, clusters, dataset_id=1)
    assert {row["strategy_family"] for row in hypotheses} == set(PHASE_2_FAMILY_NAMES)
    assert all(row["scope_type"] == "cluster" for row in hypotheses)
    assert all(row["status"] == "proposed" for row in hypotheses)
    assert all(row["test_summary"]["post_hoc"] is True for row in hypotheses)
    assert all(row["test_summary"]["confirmation_status"] == "unconfirmed" for row in hypotheses)
    assert all(row["evidence_window"]["independent_confirmation_required"] is True for row in hypotheses)
    assert all(row["test_summary"]["measurable_success_criteria"] for row in hypotheses)
    assert all(row["test_summary"]["falsification_criteria"] for row in hypotheses)
