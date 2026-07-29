"""The research funnel: broad screens rank families, focused expansions earn
elites. See app/services/labs/intraday/funnel.py.
"""

from dataclasses import replace

import pytest

from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
from app.services.labs.intraday.funnel import (
    FUNNEL_VERSION,
    SCREEN_MINIMUM_PROFIT_FACTOR,
    campaign_funnel_report,
    create_focused_expansion_campaign,
    rank_campaign_families,
    screen_campaign_families,
)

ACTIVE_FAMILY = next(arch for arch, d in FAMILY_REGISTRY.items() if d.status == "active")
OTHER_FAMILY = next(
    arch for arch, d in FAMILY_REGISTRY.items() if d.status == "archived"
)
ARCHIVED_FAMILY = next(
    arch for arch, d in FAMILY_REGISTRY.items() if d.status == "archived" and arch != OTHER_FAMILY
)


def family_row(
    architecture,
    *,
    avg_profit_factor=1.6,
    avg_expectancy=12.0,
    symbols=10,
    trades=600,
    jobs=60,
    promoted_jobs=6,
    evidence_tier="statistically_reliable",
):
    return {
        "architecture": architecture,
        "jobs": jobs,
        "promoted_jobs": promoted_jobs,
        "promotion_rate": round(promoted_jobs / jobs, 4) if jobs else 0.0,
        "symbols": symbols,
        "trades": trades,
        "trades_per_job": round(trades / jobs, 2) if jobs else 0.0,
        "avg_profit_factor": avg_profit_factor,
        "avg_expectancy": avg_expectancy,
        "avg_max_drawdown": 0.05,
        "avg_total_return": 0.2,
        "avg_holding_hours": 3.0,
        "evidence_tier": evidence_tier,
        "failure_by_validation_rule": [],
    }


@pytest.fixture
def fake_analytics(monkeypatch):
    """Patch the two stored-evidence loaders the screen is built on.

    With no trade-level evidence supplied, `screen_campaign_families` falls
    back to the average-based verdict, so these tests exercise the ranking
    rules in isolation. Pass `trades_by_family` to exercise the
    response-surface path instead.
    """

    def install(rows, trades_by_family=None, parameters_by_candidate=None):
        monkeypatch.setattr(
            "app.services.labs.intraday.strategy_analytics.campaign_family_analytics",
            lambda conn, campaign_id: list(rows),
        )
        monkeypatch.setattr(
            "app.services.labs.intraday.response_surface.load_campaign_family_trades",
            lambda conn, campaign_id: (dict(trades_by_family or {}), dict(parameters_by_candidate or {})),
        )

    return install


@pytest.fixture
def other_active_family(monkeypatch):
    """Give ranking tests a second active definition without changing policy.

    Production intentionally has one active family while Opening Repricing
    Flow is being measured. These tests still need two rows to exercise
    ordering independently of the registry's current research decision.
    """
    monkeypatch.setitem(
        FAMILY_REGISTRY,
        OTHER_FAMILY,
        replace(FAMILY_REGISTRY[OTHER_FAMILY], status="active"),
    )
    return OTHER_FAMILY


# ---------------------------------------------------------------------------
# Screening floors: "worth more compute", never "good enough to trade"
# ---------------------------------------------------------------------------

def test_a_family_with_no_edge_signal_is_not_worth_expanding(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY, avg_profit_factor=0.8)])

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["promising"] is False
    assert "NO_EDGE_SIGNAL" in ranked[0]["exclusion_reasons"]


def test_a_family_with_negative_expectancy_is_not_worth_expanding(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY, avg_expectancy=-3.0)])

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["promising"] is False
    assert "NON_POSITIVE_EXPECTANCY" in ranked[0]["exclusion_reasons"]


def test_a_family_with_insufficient_sample_is_not_worth_expanding(fake_analytics):
    """Spectacular numbers on a thin sample are still a thin sample."""
    fake_analytics(
        [family_row(ACTIVE_FAMILY, avg_profit_factor=8.0, trades=6, evidence_tier="insufficient_sample")]
    )

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["promising"] is False
    assert "INSUFFICIENT_SAMPLE" in ranked[0]["exclusion_reasons"]


def test_a_single_symbol_family_is_not_worth_expanding(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY, symbols=1)])

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["promising"] is False
    assert "TOO_FEW_SYMBOLS" in ranked[0]["exclusion_reasons"]


def test_archived_families_are_never_expanded(fake_analytics):
    """Phase 12.4 archived the v1 families after finding no edge; the standing
    instruction is to stop spending compute on them."""
    fake_analytics([family_row(ARCHIVED_FAMILY, avg_profit_factor=2.5)])

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["promising"] is False
    assert "ARCHIVED_FAMILY" in ranked[0]["exclusion_reasons"]


def test_a_family_clearing_every_floor_is_promising(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY)])

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["promising"] is True
    assert ranked[0]["exclusion_reasons"] == []
    assert ranked[0]["family_name"] == FAMILY_REGISTRY[ACTIVE_FAMILY].name
    assert ranked[0]["funnel_version"] == FUNNEL_VERSION


def test_the_screening_floor_is_far_below_the_elite_gate():
    """The floor decides where to spend compute, not what to trade. If it ever
    creeps up to the elite gate's 1.2 the funnel stops admitting the
    thin-evidence families it exists to rescue."""
    assert SCREEN_MINIMUM_PROFIT_FACTOR < 1.2


# ---------------------------------------------------------------------------
# Ranking order
# ---------------------------------------------------------------------------

def test_promising_families_rank_above_excluded_ones(fake_analytics):
    fake_analytics(
        [
            family_row(ARCHIVED_FAMILY, avg_profit_factor=9.0, trades=5000),
            family_row(ACTIVE_FAMILY, avg_profit_factor=1.3),
        ]
    )

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["architecture"] == ACTIVE_FAMILY
    assert ranked[1]["architecture"] == ARCHIVED_FAMILY


def test_stronger_edge_ranks_higher_among_promising_families(fake_analytics, other_active_family):
    fake_analytics(
        [
            family_row(ACTIVE_FAMILY, avg_profit_factor=1.2),
            family_row(other_active_family, avg_profit_factor=2.4),
        ]
    )

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["architecture"] == other_active_family
    assert ranked[0]["screen_score"] > ranked[1]["screen_score"]


def test_trade_starved_families_rank_below_equally_profitable_frequent_ones(
    fake_analytics, other_active_family
):
    """Trade starvation, not absent edge, is the documented reason intraday
    candidates fail the gate -- so frequency has to matter to the ranking."""
    fake_analytics(
        [
            family_row(ACTIVE_FAMILY, trades=60, jobs=60),
            family_row(other_active_family, trades=1800, jobs=60),
        ]
    )

    ranked = rank_campaign_families(None, 101)

    assert ranked[0]["architecture"] == other_active_family


# ---------------------------------------------------------------------------
# The screen that actually decides expansions
# ---------------------------------------------------------------------------

def _trade(candidate_id, symbol, gross, month="2026-01"):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "month_key": month,
        "gross_pnl": gross,
        "fees": 0.0,
        "slippage_cost": 0.0,
    }


def test_a_family_with_healthy_averages_but_no_stable_region_is_rejected(fake_analytics):
    """The upgrade over average-based screening: one lucky variant can carry
    a family's mean, but it is not a region and must not buy compute."""
    trades = [_trade("winner", symbol, 50.0) for symbol in ("NVDA", "TSLA") for _ in range(15)]
    trades += [_trade(loser, "NVDA", -5.0) for loser in ("l1", "l2", "l3") for _ in range(10)]
    parameters = {
        "winner": {"threshold": 1, "window": 5},
        "l1": {"threshold": 20, "window": 40},
        "l2": {"threshold": 21, "window": 41},
        "l3": {"threshold": 22, "window": 42},
    }
    fake_analytics(
        [family_row(ACTIVE_FAMILY)],
        trades_by_family={ACTIVE_FAMILY: trades},
        parameters_by_candidate=parameters,
    )

    screened = screen_campaign_families(None, 101)

    assert screened[0]["screen_basis"] == "response_surface"
    assert screened[0]["promising"] is False
    assert "NO_STABLE_PARAMETER_REGION" in screened[0]["exclusion_reasons"]


def test_a_family_with_a_genuine_plateau_passes_the_screen(fake_analytics):
    trades = []
    parameters = {}
    for index, candidate in enumerate(("c1", "c2", "c3")):
        parameters[candidate] = {"threshold": index, "window": 5}
        for symbol in ("NVDA", "TSLA"):
            for month in ("2026-01", "2026-02"):
                trades += [_trade(candidate, symbol, 30.0, month) for _ in range(4)]
                trades.append(_trade(candidate, symbol, -10.0, month))
    fake_analytics(
        [family_row(ACTIVE_FAMILY)],
        trades_by_family={ACTIVE_FAMILY: trades},
        parameters_by_candidate=parameters,
    )

    screened = screen_campaign_families(None, 101)

    assert screened[0]["promising"] is True, screened[0]["exclusion_reasons"]
    assert screened[0]["response_surface"]["stable_region"]["size"] == 3


def test_archived_families_stay_excluded_even_with_a_perfect_surface(fake_analytics):
    """Registry status is policy, not structure: a sound response surface
    does not reopen a family the team decided to stop tuning."""
    trades = []
    parameters = {}
    for index, candidate in enumerate(("c1", "c2", "c3")):
        parameters[candidate] = {"threshold": index, "window": 5}
        for symbol in ("NVDA", "TSLA"):
            for month in ("2026-01", "2026-02"):
                trades += [_trade(candidate, symbol, 30.0, month) for _ in range(4)]
                trades.append(_trade(candidate, symbol, -10.0, month))
    fake_analytics(
        [family_row(ARCHIVED_FAMILY)],
        trades_by_family={ARCHIVED_FAMILY: trades},
        parameters_by_candidate=parameters,
    )

    screened = screen_campaign_families(None, 101)

    assert screened[0]["promising"] is False
    assert "ARCHIVED_FAMILY" in screened[0]["exclusion_reasons"]


def test_a_family_without_trade_evidence_keeps_its_average_based_verdict(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY)])

    screened = screen_campaign_families(None, 101)

    assert screened[0]["screen_basis"] == "family_averages_no_trade_evidence"
    assert screened[0]["promising"] is True


# ---------------------------------------------------------------------------
# The report a broad screen should be read by
# ---------------------------------------------------------------------------

def test_the_funnel_report_states_that_a_broad_screen_is_not_expected_to_make_elites(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY), family_row(ARCHIVED_FAMILY)])

    report = campaign_funnel_report(None, 101)

    assert report["families_screened"] == 2
    assert report["families_promising"] == 1
    assert "not expected to produce elites" in report["screening_policy"]
    assert "focused multi-asset expansion" in report["next_step"]


def test_the_report_says_what_to_do_when_nothing_clears_the_floor(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY, avg_profit_factor=0.7)])

    report = campaign_funnel_report(None, 101)

    assert report["families_promising"] == 0
    assert "No family cleared" in report["next_step"]


# ---------------------------------------------------------------------------
# Focused expansion
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_launch(monkeypatch):
    calls = {}

    def fake_create(conn, **kwargs):
        calls.update(kwargs)
        return {"campaign_id": 102, "jobs_created": 480, "candidates_queued": 48}

    monkeypatch.setattr(
        "app.services.labs.intraday.families.registry.create_intraday_campaign", fake_create
    )
    return calls


def test_expansion_targets_only_the_top_ranked_promising_families(
    fake_analytics, captured_launch, other_active_family
):
    fake_analytics(
        [
            family_row(ACTIVE_FAMILY, avg_profit_factor=2.4),
            family_row(other_active_family, avg_profit_factor=1.3),
            family_row(ARCHIVED_FAMILY, avg_profit_factor=5.0),
        ]
    )

    result = create_focused_expansion_campaign(None, source_campaign_id=101, max_families=1)

    assert captured_launch["family_ids"] == [ACTIVE_FAMILY]
    assert result["source_campaign_id"] == 101
    assert [row["architecture"] for row in result["expanded_families"]] == [ACTIVE_FAMILY]
    assert ARCHIVED_FAMILY in [row["architecture"] for row in result["families_rejected"]]


def test_expansion_goes_deeper_into_the_grid_and_wider_across_assets(fake_analytics, captured_launch):
    """Depth plus the full universe is what makes the unchanged gate's
    assets_passed/trade_count requirements reachable on merit."""
    fake_analytics([family_row(ACTIVE_FAMILY)])

    create_focused_expansion_campaign(
        None, source_campaign_id=101, candidates_per_family=32, asset_limit=10
    )

    assert captured_launch["max_candidates_per_family"] == 32
    assert captured_launch["asset_limit"] == 10


def test_every_expansion_gets_a_unique_campaign_label(fake_analytics, monkeypatch):
    """Two expansions of the same winners must not collide on campaign_key and
    silently reuse the earlier campaign."""
    fake_analytics([family_row(ACTIVE_FAMILY)])
    labels = []

    def fake_create(conn, **kwargs):
        labels.append(kwargs["campaign_label"])
        return {"campaign_id": 102}

    monkeypatch.setattr(
        "app.services.labs.intraday.families.registry.create_intraday_campaign", fake_create
    )

    create_focused_expansion_campaign(None, source_campaign_id=101)
    create_focused_expansion_campaign(None, source_campaign_id=101)

    assert labels[0] != labels[1]
    assert all(label.startswith(FUNNEL_VERSION) for label in labels)


def test_expansion_refuses_when_no_family_cleared_the_floor(fake_analytics, captured_launch):
    fake_analytics([family_row(ACTIVE_FAMILY, avg_profit_factor=0.6)])

    with pytest.raises(ValueError, match="cleared the screening floor"):
        create_focused_expansion_campaign(None, source_campaign_id=101)

    assert captured_launch == {}


def test_expansion_refuses_on_a_campaign_with_nothing_to_screen(fake_analytics, captured_launch):
    fake_analytics([])

    with pytest.raises(ValueError, match="no completed jobs"):
        create_focused_expansion_campaign(None, source_campaign_id=101)

    assert captured_launch == {}


def test_refusal_explains_why_each_family_was_rejected(fake_analytics):
    fake_analytics([family_row(ACTIVE_FAMILY, avg_profit_factor=0.6)])

    with pytest.raises(ValueError) as error:
        create_focused_expansion_campaign(None, source_campaign_id=101)

    assert "NO_EDGE_SIGNAL" in str(error.value)
