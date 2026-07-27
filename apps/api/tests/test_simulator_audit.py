"""Phase A: the simulator audit must stay green.

These lock in the invariants of the shared execution path. A failure here
means `run_backtest` computes something incorrectly, and every strategy
verdict produced since the break is suspect -- so this file is the one that
should fail loudly before anyone concludes a family has no edge.
"""

import pytest

from app.services.simulator_audit import (
    ALL_CHECKS,
    AUDIT_VERSION,
    cost_break_even_analysis,
    run_simulator_audit,
)


def test_the_shared_execution_path_has_no_defects():
    """The headline verdict. If this fails, stop researching strategies."""
    audit = run_simulator_audit()

    assert audit["simulator_sound"] is True, f"simulator defects found: {audit['defects']}"
    assert audit["defects"] == []
    assert audit["audit_version"] == AUDIT_VERSION


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda check: check.__name__)
def test_each_audit_check_reports_no_defect(check):
    """Each invariant, individually, so a failure names itself."""
    result = check()

    assert result.finding_type != "defect", f"{result.name}: {result.detail} | {result.observed}"


def test_the_audit_separates_defects_from_uneconomic_configuration():
    """A configuration that cannot pay for itself is a research finding, not a
    broken backtester. Conflating the two would send us to fix code that is
    already correct."""
    audit = run_simulator_audit()

    assert audit["economics_findings"], "expected the live cost configuration to be flagged"
    assert audit["simulator_sound"] is True


def test_costs_scale_inversely_with_stop_distance():
    """The core economic result: halving the stop doubles the cost in R,
    because risk-based sizing doubles the position while the target does not
    move. This is why tight intraday stops are self-defeating under
    percentage-of-notional costs."""
    wide = cost_break_even_analysis(
        fee_rate=0.001, slippage_rate=0.0005, stop_distance_pct=0.01, reward_risk_multiple=1.5
    )
    tight = cost_break_even_analysis(
        fee_rate=0.001, slippage_rate=0.0005, stop_distance_pct=0.005, reward_risk_multiple=1.5
    )

    assert tight["cost_in_r"] == pytest.approx(wide["cost_in_r"] * 2)
    assert tight["required_win_rate_after_costs"] > wide["required_win_rate_after_costs"]


def test_a_tight_enough_stop_is_mathematically_unprofitable():
    """At 0.2% stops and these rates the required win rate reaches 100%: no
    signal quality whatsoever can rescue the configuration."""
    result = cost_break_even_analysis(
        fee_rate=0.001, slippage_rate=0.0005, stop_distance_pct=0.002, reward_risk_multiple=1.5
    )

    assert result["cost_in_r"] == pytest.approx(1.5)
    assert result["achievable"] is False


def test_zero_costs_leave_only_the_gross_break_even_requirement():
    result = cost_break_even_analysis(
        fee_rate=0.0, slippage_rate=0.0, stop_distance_pct=0.005, reward_risk_multiple=1.5
    )

    assert result["cost_in_r"] == 0.0
    assert result["required_win_rate_after_costs"] == pytest.approx(result["required_win_rate_before_costs"])
    assert result["required_win_rate_before_costs"] == pytest.approx(0.4)


def test_cost_analysis_rejects_a_nonsensical_stop_distance():
    with pytest.raises(ValueError):
        cost_break_even_analysis(
            fee_rate=0.001, slippage_rate=0.0005, stop_distance_pct=0.0, reward_risk_multiple=1.5
        )
