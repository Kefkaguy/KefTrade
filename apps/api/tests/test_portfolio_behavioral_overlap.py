"""Phase 13.8: behavioral overlap must add insight without weakening constraints."""

import pytest

from app.services.portfolio_behavioral_overlap import (
    BEHAVIORAL_REDUNDANCY_THRESHOLD,
    OVERLAP_DIMENSIONS,
    PORTFOLIO_CONCENTRATION_THRESHOLD,
    explain_infeasibility,
    pair_behavioral_overlap,
    portfolio_behavioral_diversity,
)


class FakeDnaConn:
    def __init__(self, dna_by_architecture):
        self.dna_by_architecture = dna_by_architecture

    def execute(self, query, params=None):
        architecture = params[0]
        payload = self.dna_by_architecture.get(architecture)
        if payload is None:
            return FakeResult(None)
        return FakeResult(
            {
                "id": 1,
                "family_architecture": architecture,
                "strategy_version": "v2",
                "dna_schema_version": 1,
                "fingerprint": f"fp_{architecture}",
                "dna": payload,
                "superseded_by_id": None,
                "created_at": None,
            }
        )


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def dna(**overrides):
    payload = {
        "family_architecture": "x_v2",
        "strategy_version": "v2",
        "direction_support": ["long"],
        "execution_capability": "simulation_only",
        "entry_structure": "range_breakout",
        "confirmation_structure": ["relative_volume"],
        "exit_structure": ["stop_loss"],
        "holding_horizon_class": "intraday_hours",
        "timeframe_class": "intraday_15m_30m",
        "expected_frequency_class": "roughly_daily",
        "trend_dependency": "benefits_from_trend",
        "volatility_dependency": "agnostic",
        "volume_dependency": "requires_elevated",
        "session_dependency": "first_two_hours",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["range_bound"],
        "feature_dependencies": ["atr"],
        "evidence_confidence": "untested",
    }
    payload.update(overrides)
    return payload


def candidate(candidate_id, architecture, **overrides):
    row = {
        "candidate_id": candidate_id,
        "family_id": architecture,
        "symbol": "AMD",
        "timeframe": "30m",
        "parameters": {"strategy_architecture": architecture, "direction": "long"},
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Pair overlap
# ---------------------------------------------------------------------------

def test_identical_behavior_is_flagged_redundant_even_with_different_names():
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2"), "b_v2": dna(family_architecture="b_v2")})

    result = pair_behavioral_overlap(conn, candidate("c1", "a_v2"), candidate("c2", "b_v2"))

    assert result["overlaps"]["dna_similarity"] == 1.0
    assert result["behaviorally_redundant"] is True
    assert any("behave alike" in note for note in result["notes"])


def test_genuinely_different_behavior_is_not_redundant():
    conn = FakeDnaConn({
        "a_v2": dna(family_architecture="a_v2"),
        "b_v2": dna(
            family_architecture="b_v2", behavior_class="mean_reversion", entry_structure="gap_open_fade",
            session_dependency="avoids_open", trend_dependency="requires_range",
            volume_dependency="agnostic", gap_dependency="requires_gap",
            required_regime=["range_bound"], expected_frequency_class="few_per_week",
        ),
    })

    result = pair_behavioral_overlap(conn, candidate("c1", "a_v2"), candidate("c2", "b_v2"))

    assert result["overlaps"]["dna_similarity"] < BEHAVIORAL_REDUNDANCY_THRESHOLD
    assert result["behaviorally_redundant"] is False
    assert result["overlaps"]["behavior_class_overlap"] is False


def test_missing_dna_is_reported_as_unmeasured_not_as_diverse():
    """The dangerous failure mode would be treating "we don't know" as "it's
    fine" -- that must never happen silently."""
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2")})

    result = pair_behavioral_overlap(conn, candidate("c1", "a_v2"), candidate("c2", "unknown_v2"))

    assert result["overlaps"]["dna_similarity"] is None
    assert result["behaviorally_redundant"] is False
    assert any("unmeasured" in note.lower() for note in result["notes"])
    assert any("not as diverse" in note for note in result["notes"])


def test_all_required_overlap_dimensions_are_evaluated():
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2"), "b_v2": dna(family_architecture="b_v2")})

    overlaps = pair_behavioral_overlap(conn, candidate("c1", "a_v2"), candidate("c2", "b_v2"))["overlaps"]

    assert set(overlaps) == set(OVERLAP_DIMENSIONS)


def test_symbol_timeframe_and_direction_overlap_are_detected():
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2"), "b_v2": dna(family_architecture="b_v2")})

    same = pair_behavioral_overlap(conn, candidate("c1", "a_v2"), candidate("c2", "b_v2"))["overlaps"]
    different = pair_behavioral_overlap(
        conn,
        candidate("c1", "a_v2"),
        candidate("c2", "b_v2", symbol="SPY", timeframe="15m", parameters={"strategy_architecture": "b_v2", "direction": "short"}),
    )["overlaps"]

    assert same["symbol_overlap"] is True and same["timeframe_overlap"] is True and same["direction_overlap"] is True
    assert different["symbol_overlap"] is False and different["timeframe_overlap"] is False and different["direction_overlap"] is False


def test_external_execution_eligibility_defaults_to_not_both_eligible():
    """Every V2 family is simulation_only, so no pair should read as
    externally eligible by accident."""
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2"), "b_v2": dna(family_architecture="b_v2")})

    eligibility = pair_behavioral_overlap(conn, candidate("c1", "a_v2"), candidate("c2", "b_v2"))["overlaps"][
        "external_execution_eligibility"
    ]

    assert eligibility["both_externally_eligible"] is False
    assert eligibility["left"] == "simulation_only"


# ---------------------------------------------------------------------------
# Portfolio diversity
# ---------------------------------------------------------------------------

def test_a_portfolio_of_clones_is_flagged_behaviorally_concentrated():
    conn = FakeDnaConn({f"{name}_v2": dna(family_architecture=f"{name}_v2") for name in ("a", "b", "c")})
    members = [candidate("c1", "a_v2"), candidate("c2", "b_v2"), candidate("c3", "c_v2")]

    result = portfolio_behavioral_diversity(conn, members)

    assert result["mean_pairwise_dna_similarity"] == 1.0
    assert result["assessment"] == "behaviorally_concentrated"
    assert len(result["behaviorally_redundant_pairs"]) == 3


def test_a_genuinely_mixed_portfolio_is_flagged_diverse():
    conn = FakeDnaConn({
        "a_v2": dna(family_architecture="a_v2"),
        "b_v2": dna(
            family_architecture="b_v2", behavior_class="mean_reversion", entry_structure="gap_open_fade",
            session_dependency="avoids_open", trend_dependency="requires_range", gap_dependency="requires_gap",
            required_regime=["range_bound"], volume_dependency="agnostic",
            expected_frequency_class="few_per_week", volatility_dependency="requires_normal_or_low",
            holding_horizon_class="intraday_minutes", exit_structure=["vwap_target"],
            confirmation_structure=["session_window"], direction_support=["short"],
            market_structure_dependency="requires_confirmed_structure",
            invalidation_regime=["trending_up"], feature_dependencies=["gap_atr"],
        ),
    })
    members = [candidate("c1", "a_v2"), candidate("c2", "b_v2")]

    result = portfolio_behavioral_diversity(conn, members)

    assert result["mean_pairwise_dna_similarity"] < PORTFOLIO_CONCENTRATION_THRESHOLD
    assert result["assessment"] == "behaviorally_diverse"
    assert result["behaviorally_redundant_pairs"] == []


def test_diversity_report_is_explicitly_advisory_and_changes_nothing():
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2")})
    result = portfolio_behavioral_diversity(conn, [candidate("c1", "a_v2")])

    assert result["advisory_only"] is True
    assert "not weakened" in result["constraint_note"] or "weakened" in result["constraint_note"]
    assert result["member_count"] == 1


def test_unmeasured_pairs_are_counted_separately_from_measured_ones():
    conn = FakeDnaConn({"a_v2": dna(family_architecture="a_v2")})
    members = [candidate("c1", "a_v2"), candidate("c2", "no_dna_v2")]

    result = portfolio_behavioral_diversity(conn, members)

    assert result["measured_pairs"] == 0
    assert result["unmeasured_pairs"] == 1
    assert result["assessment"] == "unmeasured"


# ---------------------------------------------------------------------------
# Infeasibility explanations
# ---------------------------------------------------------------------------

def test_feasible_portfolio_reports_no_blocking_problem():
    report = explain_infeasibility({"maximum_feasible_size": 5}, [], target_size=5)

    assert report["feasible"] is True
    assert report["alternative_profiles"] == []


def test_infeasible_portfolio_names_the_primary_blocking_constraint():
    binding = [
        {"constraint": "PARAMETER_SIMILARITY", "excluded_candidates_or_pairs": 12},
        {"constraint": "SYMBOL_FAMILY_DUPLICATE", "excluded_candidates_or_pairs": 3},
    ]
    report = explain_infeasibility({"maximum_feasible_size": 2}, binding, target_size=6)

    assert report["feasible"] is False
    assert report["primary_blocking_constraint"] == "PARAMETER_SIMILARITY"
    assert report["maximum_feasible_size"] == 2


def test_alternatives_include_a_no_constraint_change_option_first():
    binding = [{"constraint": "PARAMETER_SIMILARITY", "excluded_candidates_or_pairs": 12}]
    report = explain_infeasibility({"maximum_feasible_size": 3}, binding, target_size=8)

    smaller = report["alternative_profiles"][0]
    assert smaller["profile"] == "smaller_portfolio"
    assert smaller["requires_constraint_change"] is False
    assert "3 members is feasible" in smaller["description"]


def test_constraint_changing_alternatives_are_clearly_marked_as_suggestions():
    binding = [{"constraint": "MAXIMUM_SIGNAL_CORRELATION", "excluded_candidates_or_pairs": 9}]
    report = explain_infeasibility({"maximum_feasible_size": 1}, binding, target_size=5)

    changing = [item for item in report["alternative_profiles"] if item["requires_constraint_change"]]
    assert changing, "a blocked constraint should be surfaced for human review"
    assert "No constraint was changed" in report["constraint_note"]
    assert "human review, not an applied change" in report["constraint_note"]
