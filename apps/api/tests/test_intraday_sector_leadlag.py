import inspect

from app.services.intraday_sector_leadlag import (
    FRESH_TESTS,
    FROZEN_COST_CALIBRATION_ID,
    HORIZONS_MINUTES,
    PRIOR_EFFECTIVE_TRIALS,
    SECTOR_LEADLAG_VERSION,
    STATE_DIRECTIONS,
    STATE_NEGATIVE_PEER_IMPULSE,
    STATE_POSITIVE_PEER_IMPULSE,
    TARGET_GROSS_LOWER_BOUND_BPS,
    TOTAL_EFFECTIVE_TRIALS,
    Z_THRESHOLD,
    _build_predictor_states,
    _load_confirmation_cell_events,
    cell_passes_promotion,
    classify_peer_impulse,
    preflight_sector_leadlag,
    selection_t_threshold,
)


def _stats(lower_gross: float, lower_net: float, t_stat: float, ready: bool = True):
    return {
        "gross": {
            "block_bootstrap": {
                "confidence_interval_95": [lower_gross, lower_gross + 2.0]
            },
            "independent_evidence_ready": ready,
        },
        "net": {
            "block_bootstrap": {
                "confidence_interval_95": [lower_net, lower_net + 2.0]
            },
            "day_clustered_t_statistic": t_stat,
        },
    }


def test_protocol_is_exactly_two_states_by_three_horizons():
    assert SECTOR_LEADLAG_VERSION == "intraday_sector_leadlag_5m_v1_peer_excess_spy"
    assert Z_THRESHOLD == 1.5
    assert HORIZONS_MINUTES == (5, 10, 15)
    assert FRESH_TESTS == 6
    assert PRIOR_EFFECTIVE_TRIALS == 502
    assert TOTAL_EFFECTIVE_TRIALS == 508
    assert FROZEN_COST_CALIBRATION_ID == 4
    assert STATE_DIRECTIONS == {
        STATE_POSITIVE_PEER_IMPULSE: 1,
        STATE_NEGATIVE_PEER_IMPULSE: -1,
    }


def test_peer_impulse_classifier_uses_frozen_threshold():
    assert classify_peer_impulse(1.50) == STATE_POSITIVE_PEER_IMPULSE
    assert classify_peer_impulse(2.00) == STATE_POSITIVE_PEER_IMPULSE
    assert classify_peer_impulse(-1.50) == STATE_NEGATIVE_PEER_IMPULSE
    assert classify_peer_impulse(-2.00) == STATE_NEGATIVE_PEER_IMPULSE
    assert classify_peer_impulse(1.499) is None
    assert classify_peer_impulse(-1.499) is None


def test_trial_ledger_threshold_is_cumulative_not_fresh_only():
    assert selection_t_threshold(508) > selection_t_threshold(502)
    assert selection_t_threshold(508) > selection_t_threshold(FRESH_TESTS)
    assert abs(selection_t_threshold(508) - 3.8944416083800593) < 1e-12


def test_promotion_requires_strict_five_bps_and_positive_net_in_both_development_phases():
    assert TARGET_GROSS_LOWER_BOUND_BPS == 5.0
    discovery = _stats(5.1, 0.5, 5.0)
    validation = _stats(5.2, 0.6, 4.0)
    assert cell_passes_promotion(discovery, validation, t_threshold=3.894)

    weak_discovery = _stats(4.99, 0.5, 5.0)
    assert not cell_passes_promotion(weak_discovery, validation, t_threshold=3.894)

    weak_validation = _stats(5.2, -0.01, 5.0)
    assert not cell_passes_promotion(discovery, weak_validation, t_threshold=3.894)

    weak_t = _stats(5.2, 0.6, 3.893)
    assert not cell_passes_promotion(discovery, weak_t, t_threshold=3.894)


def test_preflight_never_calls_forward_outcome_loader():
    source = inspect.getsource(preflight_sector_leadlag)
    assert "_load_outcome_events" not in source
    assert "_load_confirmation_cell_events" not in source
    assert "forward_outcome_fields_accessed" in source


def test_predictor_builder_has_no_forward_exit_price_columns():
    source = inspect.getsource(_build_predictor_states)
    forbidden = (
        "target_exit_5m_close",
        "target_exit_10m_close",
        "target_exit_15m_close",
        "spy_exit_5m_close",
        "spy_exit_10m_close",
        "spy_exit_15m_close",
        "gross_return",
        "net_return",
    )
    for token in forbidden:
        assert token not in source


def test_confirmation_loader_is_one_cell_one_horizon_only():
    source = inspect.getsource(_load_confirmation_cell_events)
    assert "WHERE s.phase = 'confirmation'" in source
    assert "AND s.state = %(state)s" in source
    assert "exit_offset = horizon - 1" in source
    assert "target_exit_5m_close" not in source
    assert "target_exit_10m_close" not in source
    assert "target_exit_15m_close" not in source
    assert 'symbol=str(row["symbol"])' in source
    assert 'symbol="SPY"' in source
    assert "stressed=True" in source
