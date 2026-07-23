from __future__ import annotations
import pytest


from app.services.research_campaigns import (
    ELITE_MEDIAN_MINIMUM_PROFIT_FACTOR,
    ELITE_MEDIAN_MINIMUM_TRADES,
    candidate_consistency_summaries,
    cross_validation_failures,
    median,
    median_consistency_failures,
    passes_cross_validation,
    passes_single_market_gate,
)


def variant_job(candidate_id, symbol, timeframe, *, pf, gross_profit, gross_loss, expectancy, dd, trades):
    return {
        "candidate_id": candidate_id,
        "family_id": "family_test",
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "promoted",
        "validation_score": 1.0,
        "failure_reasons": [],
        "latest_error": None,
        "result": {
            "metrics": {
                "profit_factor": pf,
                "gross_profit": gross_profit,
                "gross_loss": gross_loss,
                "expectancy_per_trade": expectancy,
                "max_drawdown": dd,
                "number_of_trades": trades,
            }
        },
    }


def test_median_helper_handles_even_and_odd_and_empty() -> None:
    assert median([1, 2, 3]) == 2
    assert median([1, 2, 3, 4]) == 2.5
    assert median([]) == 0.0
    assert median([5]) == 5


def test_pooled_gate_passes_but_consistency_gate_demotes_a_lucky_variant() -> None:
    # One huge winner + four losers. Pooled PF > 1.2 (winner dominates gross totals),
    # but the MEDIAN variant loses money -> honest gate must reject.
    jobs = [
        variant_job("cand_lucky", "WIN", "1h", pf=8.0, gross_profit=8000, gross_loss=1000, expectancy=50, dd=0.03, trades=40),
        variant_job("cand_lucky", "L1", "1h", pf=0.7, gross_profit=70, gross_loss=100, expectancy=-2, dd=0.09, trades=30),
        variant_job("cand_lucky", "L2", "4h", pf=0.8, gross_profit=80, gross_loss=100, expectancy=-1, dd=0.08, trades=30),
        variant_job("cand_lucky", "L3", "1h", pf=0.75, gross_profit=75, gross_loss=100, expectancy=-1.5, dd=0.10, trades=25),
        variant_job("cand_lucky", "L4", "4h", pf=0.85, gross_profit=85, gross_loss=100, expectancy=-0.5, dd=0.07, trades=25),
    ]
    summary = candidate_consistency_summaries(jobs)[0]

    # Pooled aggregate is inflated above 1.2 by the single winner...
    assert summary["profit_factor"] >= 1.2
    assert passes_single_market_gate(summary) is True
    # ...but the typical (median) variant loses, so the honest gate rejects it.
    assert summary["median_profit_factor"] < ELITE_MEDIAN_MINIMUM_PROFIT_FACTOR
    assert "MEDIAN_PROFIT_FACTOR" in median_consistency_failures(summary)
    assert passes_cross_validation(summary) is False
    assert "MEDIAN_PROFIT_FACTOR" in cross_validation_failures(summary)


def test_genuinely_consistent_candidate_still_passes() -> None:
    jobs = [
        variant_job("cand_good", "AAA", "1h", pf=1.5, gross_profit=1500, gross_loss=1000, expectancy=5, dd=0.05, trades=40),
        variant_job("cand_good", "BBB", "4h", pf=1.4, gross_profit=1400, gross_loss=1000, expectancy=4, dd=0.06, trades=35),
        variant_job("cand_good", "CCC", "1h", pf=1.6, gross_profit=1600, gross_loss=1000, expectancy=6, dd=0.04, trades=45),
    ]
    summary = candidate_consistency_summaries(jobs)[0]

    assert summary["median_profit_factor"] >= ELITE_MEDIAN_MINIMUM_PROFIT_FACTOR
    assert summary["median_variant_trade_count"] >= ELITE_MEDIAN_MINIMUM_TRADES
    assert passes_single_market_gate(summary) is True
    assert passes_cross_validation(summary) is True
    assert cross_validation_failures(summary) == []


def test_thin_median_sample_is_rejected_even_if_profitable() -> None:
    # Profitable but the typical variant has too few trades to trust.
    jobs = [
        variant_job("cand_thin", "AAA", "1h", pf=2.0, gross_profit=200, gross_loss=100, expectancy=8, dd=0.03, trades=61),
        variant_job("cand_thin", "BBB", "4h", pf=2.0, gross_profit=40, gross_loss=20, expectancy=8, dd=0.03, trades=3),
        variant_job("cand_thin", "CCC", "1h", pf=2.0, gross_profit=40, gross_loss=20, expectancy=8, dd=0.03, trades=3),
    ]
    summary = candidate_consistency_summaries(jobs)[0]
    assert summary["median_variant_trade_count"] < ELITE_MEDIAN_MINIMUM_TRADES
    assert "MEDIAN_VARIANT_TRADES" in cross_validation_failures(summary)
    assert passes_cross_validation(summary) is False


def test_gate_only_adds_requirements_never_weakens() -> None:
    # Anything the honest gate passes must also satisfy the original aggregate gate.
    jobs = [
        variant_job("c", "AAA", "1h", pf=1.5, gross_profit=1500, gross_loss=1000, expectancy=5, dd=0.05, trades=40),
        variant_job("c", "BBB", "4h", pf=1.4, gross_profit=1400, gross_loss=1000, expectancy=4, dd=0.06, trades=35),
    ]
    summary = candidate_consistency_summaries(jobs)[0]
    if passes_cross_validation(summary):
        assert passes_single_market_gate(summary) is True


def test_reevaluation_decision_is_deterministic() -> None:
    jobs = [
        variant_job("cand_lucky", "WIN", "1h", pf=8.0, gross_profit=8000, gross_loss=1000, expectancy=50, dd=0.03, trades=40),
        variant_job("cand_lucky", "L1", "1h", pf=0.7, gross_profit=70, gross_loss=100, expectancy=-2, dd=0.09, trades=30),
        variant_job("cand_lucky", "L2", "4h", pf=0.8, gross_profit=80, gross_loss=100, expectancy=-1, dd=0.08, trades=30),
    ]
    first = candidate_consistency_summaries(jobs)[0]
    second = candidate_consistency_summaries(jobs)[0]
    assert cross_validation_failures(first) == cross_validation_failures(second)
    assert first["median_profit_factor"] == second["median_profit_factor"]


def _freq_summary(trades_per_year):
    return {
        "research_score": 70.0, "profit_factor": 1.5, "expectancy": 10.0, "max_drawdown": 0.05,
        "trade_count": 120, "stability": 1.0, "assets_passed": 2, "timeframes_passed": 1,
        "median_profit_factor": 1.4, "median_expectancy": 8.0, "median_max_drawdown": 0.05,
        "median_variant_trade_count": 40, "median_trades_per_year": trades_per_year,
    }


def test_frequency_gate_is_measure_only_by_default() -> None:
    from app.services.research_campaigns import frequency_failures, minimum_trades_per_year

    assert minimum_trades_per_year() == 0
    # A ~12 trades/year strategy is not demoted while the floor is disabled.
    assert frequency_failures(_freq_summary(12.0)) == []
    assert passes_cross_validation(_freq_summary(12.0)) is True


def test_frequency_gate_rejects_rare_strategies_once_enabled(monkeypatch) -> None:
    from app.services import research_campaigns
    from app.settings import settings

    monkeypatch.setattr(settings, "elite_minimum_trades_per_year", 50, raising=False)
    assert research_campaigns.frequency_failures(_freq_summary(12.0)) == ["MEDIAN_TRADES_PER_YEAR"]
    assert research_campaigns.passes_cross_validation(_freq_summary(12.0)) is False
    # A genuinely frequent strategy still passes.
    assert research_campaigns.passes_cross_validation(_freq_summary(180.0)) is True


def test_trade_frequency_classes() -> None:
    from app.services.research_campaigns import trade_frequency_class

    assert trade_frequency_class(300) == "daily"
    assert trade_frequency_class(60) == "weekly"
    assert trade_frequency_class(12) == "monthly"
    assert trade_frequency_class(11) == "rare"


def test_trades_per_year_annualizes_from_walk_forward_window() -> None:
    from app.services.research_campaigns import trades_per_year_for_metrics

    metrics = {
        "number_of_trades": 40,
        "walk_forward": {"train_start": "2024-01-01T00:00:00+00:00", "validation_end": "2026-01-01T00:00:00+00:00"},
    }
    # Jan-2024 -> Jan-2026 spans 731 days (2024 is a leap year), so ~2.001 years.
    assert trades_per_year_for_metrics(metrics) == pytest.approx(20.0, rel=0.01)
    assert trades_per_year_for_metrics({"number_of_trades": 40}) is None
