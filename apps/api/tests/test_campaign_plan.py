"""The launch preview and the launch itself must resolve identically."""

import pytest

from app.services.labs.intraday.campaign_plan import (
    CAMPAIGN_PLAN_VERSION,
    active_family_definitions,
    build_campaign_plan,
)

UNIVERSE = ["AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA"]


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class FakePlanConn:
    def __init__(self, assets=None):
        self.assets = UNIVERSE if assets is None else assets

    def execute(self, query, params=None):
        if "research_universes" in query or "assets" in query:
            return FakeResult({"assets": self.assets})
        return FakeResult(None)

    def commit(self):
        pass


def test_only_active_families_are_planned():
    """Archived families keep their evidence and stay visible, but a broad
    screen must never spend compute on them."""
    families = active_family_definitions()

    assert families
    assert all(family["status"] == "active" for family in families)


def test_the_plan_reports_the_resolved_family_count_not_a_constant():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["15m", "30m"])

    assert plan["active_family_count"] == len(active_family_definitions())
    assert plan["plan_version"] == CAMPAIGN_PLAN_VERSION


def test_job_count_is_families_by_assets_by_timeframes_after_dedupe():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["15m", "30m"])

    assert plan["estimated_jobs"] == (
        plan["candidates_after_dedupe"] * plan["asset_count"] * len(plan["timeframes_selected"])
    )


def test_selecting_one_timeframe_halves_the_job_count():
    both = build_campaign_plan(FakePlanConn(), timeframes=["15m", "30m"])
    one = build_campaign_plan(FakePlanConn(), timeframes=["30m"])

    assert one["estimated_jobs"] * 2 == both["estimated_jobs"]
    assert one["timeframes_selected"] == ["30m"]


def test_selecting_no_timeframe_blocks_the_launch():
    plan = build_campaign_plan(FakePlanConn(), timeframes=[])

    assert plan["can_launch"] is False
    assert any(item["code"] == "NO_TIMEFRAME_SELECTED" for item in plan["blockers"])


def test_an_unsupported_timeframe_is_dropped_rather_than_trusted():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["15m", "1d"])

    assert plan["timeframes_selected"] == ["15m"]


def test_omitting_timeframes_selects_every_supported_one():
    plan = build_campaign_plan(FakePlanConn(), timeframes=None)

    assert plan["timeframes_selected"] == plan["timeframes_supported"]


def test_an_empty_universe_blocks_the_launch():
    plan = build_campaign_plan(FakePlanConn(assets=[]), timeframes=["30m"])

    assert plan["can_launch"] is False
    assert any(item["code"] == "NO_ASSETS_RESOLVED" for item in plan["blockers"])


def test_the_asset_limit_is_respected():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["30m"], asset_limit=4)

    assert plan["asset_count"] == 4


def test_more_variants_per_family_means_more_jobs():
    small = build_campaign_plan(FakePlanConn(), timeframes=["30m"], variants_per_family=4)
    large = build_campaign_plan(FakePlanConn(), timeframes=["30m"], variants_per_family=12)

    assert large["estimated_jobs"] > small["estimated_jobs"]
    assert large["variants_per_family"] == 12


def test_the_plan_reports_the_protocol_versions_in_force():
    from app.services.research_campaigns import ELITE_PROMOTION_RULE_VERSION
    from app.services.research_splits import SPLIT_VERSION

    protocol = build_campaign_plan(FakePlanConn(), timeframes=["30m"])["protocol"]

    assert protocol["split_protocol_version"] == SPLIT_VERSION
    assert protocol["elite_gate_version"] == ELITE_PROMOTION_RULE_VERSION
    assert protocol["cost_model"]["round_trip_rate"] > 0


def test_the_cost_model_version_is_derived_from_the_live_rates():
    """A stored label could go stale against a changed cost assumption."""
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    cost = build_campaign_plan(FakePlanConn(), timeframes=["30m"])["protocol"]["cost_model"]

    assert cost["fee_rate_per_leg"] == float(BASE_V2_PARAMETERS["fee_rate"])
    assert str(cost["fee_rate_per_leg"]).rstrip("0").rstrip(".") in cost["version"]


def test_a_sound_simulator_produces_no_blocker():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["30m"])

    assert plan["protocol"]["simulator_audit"]["simulator_sound"] is True
    assert not any(item["code"] == "SIMULATOR_DEFECT" for item in plan["blockers"])


def test_the_uneconomic_cost_finding_warns_without_blocking():
    """The screen may still run; the caveat is that weak results could reflect
    the cost model rather than the strategies."""
    plan = build_campaign_plan(FakePlanConn(), timeframes=["30m"])

    assert any(item["code"] == "COST_MODEL_UNECONOMIC" for item in plan["warnings"])
    assert plan["can_launch"] is True


def test_the_plan_restates_the_evidence_separation_rule():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["30m"])

    assert "never merged across families" in plan["evidence_policy"]


def test_a_preview_survives_an_unreadable_universe():
    """A preview that raises is worse than one that reports no assets and
    blocks the launch."""

    class BrokenConn:
        def execute(self, query, params=None):
            raise RuntimeError("universe table unavailable")

        def commit(self):
            pass

    plan = build_campaign_plan(BrokenConn(), timeframes=["30m"])

    assert plan["asset_count"] == 0
    assert plan["can_launch"] is False
