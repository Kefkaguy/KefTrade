"""Phase 13.5 + 13.9: analytics evidence tiers, buckets, and explainability."""

import pytest

from app.services.labs.intraday.strategy_analytics import (
    EVIDENCE_TIER_RULES,
    MINIMUM_TRADES_FOR_DESCRIPTIVE,
    MINIMUM_TRADES_FOR_EXPLORATORY,
    MINIMUM_TRADES_FOR_RELIABLE,
    _mean_confidence_interval,
    campaign_family_analytics,
    candidate_buckets,
    evidence_tier,
)


class FakeAnalyticsConn:
    """Routes each query to a canned result set by matching on a fragment."""

    def __init__(self, routes):
        self.routes = routes

    def execute(self, query, params=None):
        for fragment, rows in self.routes.items():
            if fragment in query:
                return FakeResult(rows)
        return FakeResult([])


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


# ---------------------------------------------------------------------------
# Evidence tiers
# ---------------------------------------------------------------------------

def test_evidence_tier_requires_both_trade_count_and_symbol_spread_for_reliability():
    assert evidence_tier(MINIMUM_TRADES_FOR_RELIABLE, 2) == "statistically_reliable"
    # Same trade count, single symbol -> not reliable, merely descriptive.
    assert evidence_tier(MINIMUM_TRADES_FOR_RELIABLE, 1) == "descriptive"


def test_evidence_tier_ladders_down_with_sample_size():
    assert evidence_tier(MINIMUM_TRADES_FOR_DESCRIPTIVE, 1) == "descriptive"
    assert evidence_tier(MINIMUM_TRADES_FOR_EXPLORATORY, 1) == "exploratory"
    assert evidence_tier(MINIMUM_TRADES_FOR_EXPLORATORY - 1, 5) == "insufficient_sample"
    assert evidence_tier(0, 0) == "insufficient_sample"


def test_a_spectacular_result_on_a_tiny_sample_is_still_insufficient():
    """Tier must depend on sample size and spread only -- never on how good
    the numbers look."""
    assert evidence_tier(4, 1) == "insufficient_sample"


def test_evidence_tier_rules_are_exposed_for_the_ui():
    assert set(EVIDENCE_TIER_RULES) >= {
        "statistically_reliable", "descriptive", "exploratory", "insufficient_sample", "note",
    }
    assert "never how favorable" in EVIDENCE_TIER_RULES["note"]


# ---------------------------------------------------------------------------
# Confidence intervals
# ---------------------------------------------------------------------------

def test_confidence_interval_brackets_the_mean_and_reports_sample_size():
    ci = _mean_confidence_interval([10.0, 12.0, 8.0, 11.0, 9.0])

    assert ci["lower"] < ci["mean"] < ci["upper"]
    assert ci["sample_size"] == 5
    assert ci["reliable"] is False  # below the 30-observation floor


def test_confidence_interval_is_flagged_reliable_only_with_enough_observations():
    ci = _mean_confidence_interval([1.0] * MINIMUM_TRADES_FOR_DESCRIPTIVE)
    assert ci["reliable"] is True


def test_confidence_interval_handles_degenerate_samples():
    assert _mean_confidence_interval([])["mean"] is None
    single = _mean_confidence_interval([5.0])
    assert single["mean"] == 5.0
    assert single["lower"] is None
    assert single["reliable"] is False


# ---------------------------------------------------------------------------
# Family analytics
# ---------------------------------------------------------------------------

def test_family_analytics_computes_promotion_rate_and_tier():
    conn = FakeAnalyticsConn({
        "GROUP BY 1\n        ORDER BY 1": [
            {
                "architecture": "gap_fill_v2", "jobs": 20, "promoted": 4, "symbols": 3, "trades": 120,
                "avg_profit_factor": 1.31, "avg_expectancy": 4.2, "avg_max_drawdown": 0.08,
                "avg_total_return": 0.05, "avg_holding_hours": 2.5,
            }
        ],
        "jsonb_array_elements_text": [
            {"architecture": "gap_fill_v2", "reason": "weak_profit_factor", "occurrences": 9},
        ],
    })

    analytics = campaign_family_analytics(conn, 99)
    row = analytics[0]

    assert row["promotion_rate"] == 0.2
    assert row["trades_per_job"] == 6.0
    assert row["evidence_tier"] == "statistically_reliable"
    assert row["failure_by_validation_rule"][0]["validation_rule"] == "weak_profit_factor"


def test_family_analytics_handles_a_family_with_no_trades():
    conn = FakeAnalyticsConn({
        "GROUP BY 1\n        ORDER BY 1": [
            {
                "architecture": "quiet_v2", "jobs": 8, "promoted": 0, "symbols": 2, "trades": 0,
                "avg_profit_factor": None, "avg_expectancy": None, "avg_max_drawdown": None,
                "avg_total_return": None, "avg_holding_hours": None,
            }
        ],
    })

    row = campaign_family_analytics(conn, 99)[0]
    assert row["trades"] == 0
    assert row["evidence_tier"] == "insufficient_sample"
    assert row["avg_profit_factor"] is None
    assert row["promotion_rate"] == 0.0


# ---------------------------------------------------------------------------
# Candidate buckets
# ---------------------------------------------------------------------------

def job_row(**overrides):
    row = {
        "candidate_id": "c1", "architecture": "gap_fill_v2", "symbol": "AMD", "timeframe": "30m",
        "status": "rejected", "profit_factor": 1.0, "expectancy": 0.0, "trades": 40,
        "max_drawdown": 0.05, "failure_reasons": [],
    }
    row.update(overrides)
    return row


def test_profitable_but_under_evidenced_bucket_requires_a_short_sample():
    conn = FakeAnalyticsConn({"FROM research_campaign_jobs": [
        job_row(candidate_id="thin", profit_factor=1.8, expectancy=12.0, trades=12),
        job_row(candidate_id="thick", profit_factor=1.8, expectancy=12.0, trades=90),
    ]})

    buckets = candidate_buckets(conn, 99)
    ids = [item["candidate_id"] for item in buckets["profitable_but_under_evidenced"]]

    assert ids == ["thin"], "a well-evidenced profitable candidate is not 'under-evidenced'"
    assert "only 12 trades" in buckets["profitable_but_under_evidenced"][0]["why"]


def test_frequent_but_unprofitable_bucket_requires_many_trades_and_pf_below_one():
    conn = FakeAnalyticsConn({"FROM research_campaign_jobs": [
        job_row(candidate_id="churner", profit_factor=0.6, trades=200),
        job_row(candidate_id="rare_loser", profit_factor=0.6, trades=12),
    ]})

    ids = [item["candidate_id"] for item in candidate_buckets(conn, 99)["frequent_but_unprofitable"]]
    assert ids == ["churner"]


def test_near_pass_bucket_sits_just_below_the_unchanged_gate():
    conn = FakeAnalyticsConn({"FROM research_campaign_jobs": [
        job_row(candidate_id="near", profit_factor=1.15, trades=45),
        job_row(candidate_id="far", profit_factor=0.7, trades=45),
        job_row(candidate_id="passed", profit_factor=1.35, trades=45, status="promoted"),
    ]})

    near = candidate_buckets(conn, 99)["near_pass"]
    ids = [item["candidate_id"] for item in near]

    assert ids == ["near"]
    assert "1.2 gate" in near[0]["why"], "the explanation must name the unchanged threshold"


def test_near_pass_never_includes_already_promoted_candidates():
    conn = FakeAnalyticsConn({"FROM research_campaign_jobs": [
        job_row(candidate_id="promoted_near", profit_factor=1.1, trades=45, status="promoted"),
    ]})

    assert candidate_buckets(conn, 99)["near_pass"] == []


def test_buckets_ignore_jobs_without_metrics():
    conn = FakeAnalyticsConn({"FROM research_campaign_jobs": [
        job_row(candidate_id="nometrics", profit_factor=None, expectancy=None, trades=0),
    ]})

    buckets = candidate_buckets(conn, 99)
    assert buckets["profitable_but_under_evidenced"] == []
    assert buckets["frequent_but_unprofitable"] == []
    assert buckets["near_pass"] == []
