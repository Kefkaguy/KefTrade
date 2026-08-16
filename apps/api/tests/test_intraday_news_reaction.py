from app.services.intraday_news_reaction import (
    STATE_NEGATIVE_CONTINUATION,
    STATE_NEGATIVE_FAILURE,
    STATE_POSITIVE_CONTINUATION,
    STATE_POSITIVE_FAILURE,
    cell_passes_promotion,
    classify_news_polarity,
    classify_state,
    selection_t_threshold,
)
from app.services.intraday_news_reaction_governed import _return_blind_event_supply
from app.services.intraday_news_reaction_v2 import (
    ACTIVE_STATE_DIRECTIONS,
    FRESH_TESTS,
    NEWS_REACTION_VERSION,
    POWER_PRUNING_EVIDENCE,
)


def _stats(lower_gross: float, lower_net: float, t_stat: float, ready: bool = True):
    return {
        "gross": {
            "block_bootstrap": {"confidence_interval_95": [lower_gross, lower_gross + 2.0]},
            "independent_evidence_ready": ready,
        },
        "net": {
            "block_bootstrap": {"confidence_interval_95": [lower_net, lower_net + 2.0]},
            "day_clustered_t_statistic": t_stat,
        },
    }


def test_polarity_uses_existing_point_in_time_keyword_rules():
    polarity, positive, negative = classify_news_polarity(
        "Company raises guidance after record revenue growth", None, None
    )
    assert polarity == "positive"
    assert positive > negative

    polarity, positive, negative = classify_news_polarity(
        "Company cuts outlook amid weak results and investigation", None, None
    )
    assert polarity == "negative"
    assert negative > positive


def test_polarity_tie_or_no_signal_is_neutral():
    polarity, _, _ = classify_news_polarity("Board schedules annual meeting", None, None)
    assert polarity is None


def test_state_classifier_still_defines_all_predictor_states():
    # The shared classifier remains symmetric so the predictor-side power audit
    # is reproducible. V2 decides which states are allowed to consume outcome
    # trials separately, before any forward target is read.
    assert classify_state("positive", 0.001) == STATE_POSITIVE_CONTINUATION
    assert classify_state("positive", -0.001) == STATE_POSITIVE_FAILURE
    assert classify_state("positive", 0.0) == STATE_POSITIVE_FAILURE
    assert classify_state("negative", -0.001) == STATE_NEGATIVE_CONTINUATION
    assert classify_state("negative", 0.001) == STATE_NEGATIVE_FAILURE
    assert classify_state("negative", 0.0) == STATE_NEGATIVE_FAILURE
    assert classify_state(None, 0.001) is None


def test_v2_power_prunes_negative_states_before_outcome_trials():
    assert NEWS_REACTION_VERSION == "intraday_news_reaction_5m_v2_positive_only_power_pruned"
    assert FRESH_TESTS == 8
    assert set(ACTIVE_STATE_DIRECTIONS) == {
        STATE_POSITIVE_CONTINUATION,
        STATE_POSITIVE_FAILURE,
    }
    excluded = set(POWER_PRUNING_EVIDENCE["decision"]["excluded_underpowered"])
    assert excluded == {STATE_NEGATIVE_CONTINUATION, STATE_NEGATIVE_FAILURE}
    assert POWER_PRUNING_EVIDENCE["basis"] == "predictor_side_state_supply_only_no_forward_outcomes"


def test_selection_threshold_rises_with_cumulative_search_budget():
    fresh_only = selection_t_threshold(8)
    cumulative = selection_t_threshold(502)
    assert fresh_only > 2.0
    assert cumulative > fresh_only
    assert cumulative > 3.5


def test_promotion_requires_five_bps_on_bootstrap_lower_bound_not_point_estimate():
    discovery = _stats(5.1, 2.0, 5.0)
    validation = _stats(5.2, 2.1, 4.2)
    assert cell_passes_promotion(discovery, validation, t_threshold=3.8)

    validation["gross"]["block_bootstrap"]["confidence_interval_95"] = [4.99, 20.0]
    assert not cell_passes_promotion(discovery, validation, t_threshold=3.8)


def test_promotion_requires_positive_net_lower_bound_and_selection_adjusted_t():
    discovery = _stats(5.5, 1.0, 5.0)
    validation = _stats(5.5, -0.01, 5.0)
    assert not cell_passes_promotion(discovery, validation, t_threshold=3.8)

    validation = _stats(5.5, 1.0, 3.79)
    assert not cell_passes_promotion(discovery, validation, t_threshold=3.8)


def test_governed_preflight_source_does_not_reference_forward_ohlc_fields():
    import inspect

    source = inspect.getsource(_return_blind_event_supply)
    forbidden = (
        "reaction_open",
        "reaction_close",
        "entry_open",
        "exit_5m_close",
        "exit_10m_close",
        "exit_15m_close",
        "exit_30m_close",
        "SELECT open",
        "SELECT close",
        "target_return",
    )
    for token in forbidden:
        assert token not in source
