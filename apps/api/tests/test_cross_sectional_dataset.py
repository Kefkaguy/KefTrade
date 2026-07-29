"""Cross-sectional dispatch and dataset loading: proves the new job path is
additive and never intercepts any of the existing single-symbol families.
"""

from app.services.labs.intraday.cross_sectional_dataset import (
    CROSS_SECTIONAL_ARCHITECTURES,
    is_cross_sectional_candidate,
)
from app.services.labs.intraday.families.v2.families import V2_ARCHITECTURES


def candidate(architecture):
    return {"parameters": {"strategy_architecture": architecture}}


def test_all_three_cross_sectional_families_route_to_the_cross_sectional_path():
    assert is_cross_sectional_candidate(candidate("cross_sectional_momentum_v2")) is True
    assert is_cross_sectional_candidate(candidate("cross_sectional_reversal_v2")) is True
    assert is_cross_sectional_candidate(candidate("same_slot_institutional_flow_v1")) is True

    other_families = [
        arch for arch in V2_ARCHITECTURES if arch not in CROSS_SECTIONAL_ARCHITECTURES
    ]
    assert len(other_families) == 16, "sanity check: the other sixteen single-symbol families must still exist"
    for architecture in other_families:
        assert is_cross_sectional_candidate(candidate(architecture)) is False


def test_a_candidate_with_no_recognized_architecture_is_not_cross_sectional():
    assert is_cross_sectional_candidate({"parameters": {}}) is False
    assert is_cross_sectional_candidate({}) is False


def test_cross_sectional_architectures_set_matches_the_registered_families():
    assert CROSS_SECTIONAL_ARCHITECTURES == {
        "cross_sectional_momentum_v2",
        "cross_sectional_reversal_v2",
        "same_slot_institutional_flow_v1",
    }
