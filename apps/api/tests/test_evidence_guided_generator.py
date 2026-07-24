"""Phase 13.6: evidence-guided generator -- allocation, boundary, provenance."""

import pytest

from app.services.labs.intraday.evidence_guided_generator import (
    ALLOCATION,
    EVIDENCE_BOUNDARY,
    GENERATOR_VERSION,
    MINIMUM_JOBS_FOR_DEAD_END,
    MINIMUM_JOBS_FOR_REGION_EVIDENCE,
    FamilyEvidence,
    allocate_candidate_budget,
    generate_evidence_guided_candidates,
    load_family_evidence,
)


class FakeEvidenceConn:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None
        self.last_params = None

    def execute(self, query, params=None):
        self.last_query = query
        self.last_params = params
        return FakeResult(self.rows)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def evidence_row(architecture, jobs, promoted, pf=1.1, expectancy=5.0):
    return {
        "architecture": architecture,
        "jobs": jobs,
        "promoted": promoted,
        "median_profit_factor": pf,
        "median_expectancy": expectancy,
    }


# ---------------------------------------------------------------------------
# Allocation is fixed and versioned
# ---------------------------------------------------------------------------

def test_allocation_is_exactly_fifty_thirty_twenty():
    assert ALLOCATION == {"exploitation": 0.50, "diversity_mutation": 0.30, "exploration": 0.20}
    assert sum(ALLOCATION.values()) == pytest.approx(1.0)


def test_generator_version_names_the_allocation_so_it_cannot_change_silently():
    assert "50_30_20" in GENERATOR_VERSION


def test_budget_split_is_deterministic_and_totals_exactly():
    for total in (10, 20, 37, 100, 101):
        budget = allocate_candidate_budget(total)
        assert sum(budget.values()) == total
        assert budget == allocate_candidate_budget(total)


def test_rounding_remainder_goes_to_exploration_not_exploitation():
    """Rounding must never quietly inflate the exploitation share."""
    budget = allocate_candidate_budget(101)
    assert budget["exploitation"] == 50
    assert budget["diversity_mutation"] == 30
    assert budget["exploration"] == 21


# ---------------------------------------------------------------------------
# Evidence boundary
# ---------------------------------------------------------------------------

def test_evidence_query_reads_completed_campaigns_only():
    conn = FakeEvidenceConn([])
    load_family_evidence(conn)

    assert "c.status = 'completed'" in conn.last_query


def test_evidence_query_never_separates_validation_from_training_results():
    """Selecting on validation-window performance would contaminate the
    hold-out the elite gate depends on."""
    conn = FakeEvidenceConn([])
    load_family_evidence(conn)

    lowered = conn.last_query.lower()
    assert "validation" not in lowered
    assert "train_metrics" not in lowered
    assert "dataset_split" not in lowered


def test_current_campaign_can_be_excluded_so_it_never_informs_itself():
    conn = FakeEvidenceConn([])
    load_family_evidence(conn, exclude_campaign_id=77)

    assert conn.last_params == (77, 77)
    assert "j.campaign_id <> " in conn.last_query


def test_evidence_boundary_is_documented_in_the_response_payload():
    assert EVIDENCE_BOUNDARY["reads"]
    assert EVIDENCE_BOUNDARY["never_reads"]
    assert any("validation" in item for item in EVIDENCE_BOUNDARY["never_reads"])


# ---------------------------------------------------------------------------
# Evidence classification
# ---------------------------------------------------------------------------

def test_small_samples_are_not_treated_as_evidence():
    thin = FamilyEvidence("x_v2", jobs=MINIMUM_JOBS_FOR_REGION_EVIDENCE - 1, promoted=1, median_profit_factor=3.0, median_expectancy=50.0)
    assert thin.has_usable_evidence is False


def test_a_family_with_many_jobs_and_no_promotions_is_a_dead_end():
    dead = FamilyEvidence("dead_v2", jobs=MINIMUM_JOBS_FOR_DEAD_END, promoted=0, median_profit_factor=0.4, median_expectancy=-20.0)
    assert dead.is_dead_end is True
    assert dead.promotion_rate == 0.0


def test_a_family_with_few_jobs_and_no_promotions_is_not_yet_a_dead_end():
    """Absence of evidence is not evidence of absence -- a family must be
    genuinely tested before it is written off."""
    untested = FamilyEvidence("new_v2", jobs=MINIMUM_JOBS_FOR_DEAD_END - 1, promoted=0, median_profit_factor=None, median_expectancy=None)
    assert untested.is_dead_end is False


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def test_with_no_prior_evidence_everything_is_exploration_and_is_reported_as_such():
    """The honest state before the first completed V2 campaign. The
    allocation is not silently rewritten -- it is reported as inapplicable."""
    conn = FakeEvidenceConn([])
    result = generate_evidence_guided_candidates(
        conn, architectures=["gap_fill_v2", "vwap_bounce_v2"], total_candidates=20
    )

    channels = {entry["generation_channel"] for entry in result["provenance"]}
    assert channels == {"exploration"}
    assert result["allocated_budget"]["exploitation"] == 0
    assert result["families_with_usable_evidence"] == []
    # The declared allocation is still reported unchanged.
    assert result["allocation"] == ALLOCATION


def test_families_with_evidence_receive_exploitation_and_diversity_capacity():
    conn = FakeEvidenceConn([
        evidence_row("gap_fill_v2", jobs=40, promoted=6, pf=1.6),
        evidence_row("vwap_bounce_v2", jobs=40, promoted=3, pf=1.2),
    ])
    result = generate_evidence_guided_candidates(
        conn, architectures=["gap_fill_v2", "vwap_bounce_v2"], total_candidates=40
    )

    channels = {entry["generation_channel"] for entry in result["provenance"]}
    assert "exploitation" in channels
    assert "diversity_mutation" in channels
    assert "exploration" in channels
    assert set(result["families_with_usable_evidence"]) == {"gap_fill_v2", "vwap_bounce_v2"}


def test_dead_end_families_lose_exploitation_but_keep_exploration():
    """A dead end is not a banned idea -- reserved exploration capacity is
    the mechanism that lets a written-off family be revisited."""
    conn = FakeEvidenceConn([
        evidence_row("gap_fill_v2", jobs=MINIMUM_JOBS_FOR_DEAD_END, promoted=0, pf=0.4),
        evidence_row("vwap_bounce_v2", jobs=40, promoted=5, pf=1.5),
    ])
    result = generate_evidence_guided_candidates(
        conn, architectures=["gap_fill_v2", "vwap_bounce_v2"], total_candidates=40
    )

    assert result["dead_end_families"] == ["gap_fill_v2"]
    assert result["families_with_usable_evidence"] == ["vwap_bounce_v2"]

    by_family = {}
    for entry in result["provenance"]:
        by_family.setdefault(entry["architecture"], set()).add(entry["generation_channel"])
    assert by_family["gap_fill_v2"] == {"exploration"}
    assert "exploitation" in by_family["vwap_bounce_v2"]


def test_every_candidate_records_why_it_was_generated():
    conn = FakeEvidenceConn([evidence_row("gap_fill_v2", jobs=40, promoted=5, pf=1.5)])
    result = generate_evidence_guided_candidates(
        conn, architectures=["gap_fill_v2"], total_candidates=20
    )

    assert result["candidates"]
    for candidate in result["candidates"]:
        assert candidate.parameters["generation_channel"] in ALLOCATION
        assert candidate.parameters["generator_version"] == GENERATOR_VERSION
        reason = candidate.parameters["generation_reason"]
        assert len(reason) > 20
        assert "gap_fill_v2" in reason


def test_exploitation_reasons_cite_the_actual_evidence():
    conn = FakeEvidenceConn([evidence_row("gap_fill_v2", jobs=44, promoted=7, pf=1.55)])
    result = generate_evidence_guided_candidates(
        conn, architectures=["gap_fill_v2"], total_candidates=20
    )

    exploitation = [entry for entry in result["provenance"] if entry["generation_channel"] == "exploitation"]
    assert exploitation
    assert "1.55" in exploitation[0]["generation_reason"]
    assert "44" in exploitation[0]["generation_reason"]


def test_unknown_architecture_is_rejected_rather_than_silently_skipped():
    conn = FakeEvidenceConn([])
    with pytest.raises(ValueError, match="Unknown architectures"):
        generate_evidence_guided_candidates(conn, architectures=["not_a_family"], total_candidates=10)


def test_generation_is_deterministic():
    conn = FakeEvidenceConn([evidence_row("gap_fill_v2", jobs=40, promoted=5, pf=1.5)])
    first = generate_evidence_guided_candidates(conn, architectures=["gap_fill_v2"], total_candidates=20)
    second = generate_evidence_guided_candidates(conn, architectures=["gap_fill_v2"], total_candidates=20)

    assert [c.candidate_id for c in first["candidates"]] == [c.candidate_id for c in second["candidates"]]
    assert first["provenance"] == second["provenance"]
