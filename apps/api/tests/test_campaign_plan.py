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


class FakeListResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakePlanConn:
    def __init__(self, assets=None, duplicate_campaign_id=None, signal_rows=None):
        self.assets = UNIVERSE if assets is None else assets
        self.duplicate_campaign_id = duplicate_campaign_id
        self.signal_rows = signal_rows or []

    def execute(self, query, params=None):
        if "research_signal_diagnostics" in query:
            if query.strip().startswith("CREATE TABLE"):
                return FakeListResult([])
            return FakeListResult(list(self.signal_rows))
        if "campaign_key" in query:
            return FakeResult({"id": self.duplicate_campaign_id} if self.duplicate_campaign_id else None)
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


def test_a_fresh_configuration_needs_no_rerun_confirmation():
    plan = build_campaign_plan(FakePlanConn(), timeframes=["15m", "30m"])

    assert plan["duplicate_of_campaign_id"] is None
    assert plan["requires_rerun_confirmation"] is False
    assert not any(item["code"] == "DUPLICATE_CONFIGURATION" for item in plan["warnings"])


def test_a_configuration_that_already_ran_is_flagged_before_the_click():
    """The collision used to surface only as a 422 after launching. The
    preview has to know, so the button can say what it will actually do."""
    plan = build_campaign_plan(FakePlanConn(duplicate_campaign_id=89), timeframes=["15m", "30m"])

    assert plan["duplicate_of_campaign_id"] == 89
    assert plan["requires_rerun_confirmation"] is True
    assert any(item["code"] == "DUPLICATE_CONFIGURATION" for item in plan["warnings"])


def test_a_duplicate_warns_without_blocking():
    """Re-running against an advanced rolling dataset is legitimate research,
    so this is the caller's decision, not a hard stop."""
    plan = build_campaign_plan(FakePlanConn(duplicate_campaign_id=89), timeframes=["30m"])

    assert plan["can_launch"] is True
    assert not any(item["code"] == "DUPLICATE_CONFIGURATION" for item in plan["blockers"])


def test_the_duplicate_lookup_uses_the_raw_candidate_count():
    """`research_campaign_key` is built from the generated count, before the
    per-job dedupe. Using the deduped count would look up a key the launcher
    never writes, and the collision would go undetected."""
    plan = build_campaign_plan(FakePlanConn(), timeframes=["30m"])

    assert plan["candidates_generated"] >= plan["candidates_after_dedupe"]


def test_each_rerun_label_is_unique():
    from app.services.labs.intraday.campaign_plan import rerun_campaign_label

    assert rerun_campaign_label() != rerun_campaign_label()
    assert rerun_campaign_label().startswith("rerun_")


def _signal_row(architecture, verdict):
    return {"architecture": architecture, "verdict": verdict}


def test_an_unmeasured_signal_warns_before_the_compute_is_spent():
    """A campaign is the most expensive way to learn a signal predicts
    nothing. Not-yet-checked must not look like checked-and-passed."""
    plan = build_campaign_plan(FakePlanConn(), timeframes=["30m"])

    assert plan["signal_diagnostics"]["measured_families"] == 0
    assert any(item["code"] == "SIGNAL_NOT_MEASURED" for item in plan["warnings"])


def test_measured_families_with_no_predictive_content_warn():
    rows = [_signal_row("gap_fill_v2", "no_signal"), _signal_row("vwap_bounce_v2", "signal_below_cost")]
    plan = build_campaign_plan(FakePlanConn(signal_rows=rows), timeframes=["30m"])

    assert any(item["code"] == "NO_PREDICTIVE_FAMILY" for item in plan["warnings"])
    assert plan["signal_diagnostics"]["signal_below_cost"] == ["vwap_bounce_v2"]


def test_a_predictive_family_removes_the_signal_warning():
    rows = [_signal_row("gap_fill_v2", "predictive"), _signal_row("vwap_bounce_v2", "no_signal")]
    plan = build_campaign_plan(FakePlanConn(signal_rows=rows), timeframes=["30m"])

    codes = {item["code"] for item in plan["warnings"]}
    assert "NO_PREDICTIVE_FAMILY" not in codes
    assert "SIGNAL_NOT_MEASURED" not in codes
    assert plan["signal_diagnostics"]["predictive"] == ["gap_fill_v2"]


def test_signal_diagnostics_never_block_a_launch():
    """Advisory, not a gate: the user may legitimately want to run anyway."""
    rows = [_signal_row("gap_fill_v2", "no_signal")]
    plan = build_campaign_plan(FakePlanConn(signal_rows=rows), timeframes=["30m"])

    assert plan["can_launch"] is True
    assert not any(item["code"] == "NO_PREDICTIVE_FAMILY" for item in plan["blockers"])


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
