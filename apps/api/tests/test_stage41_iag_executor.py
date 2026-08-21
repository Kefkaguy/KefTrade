"""Stage 4.1 IAG-v1 tests.

The qualification logic is exercised against constructed windows rather than
production data, so the *refusal* cases can be checked — an event that fails to
qualify is the common outcome here, and a suite that only runs where real data
exists could never test one.

The governance boundary gets structural tests, not textual ones: this module's
prose necessarily discusses forward returns and P&L, so a keyword search would
match the explanation and prove nothing.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import json

import numpy as np
import pytest

from app.services.stage41_iag_executor import (
    FAIL_ABSORBED,
    FAIL_AMBIGUOUS_DIRECTION,
    FAIL_NO_DEPLETION,
    FAIL_REPLENISHED,
    FAIL_SUPPORT,
    FAIL_THIN_BASELINE,
    FAIL_THIN_WINDOW,
    FAILURE_REASONS,
    EventState,
    build_baseline,
    count_specification,
    decide_verdict,
    gross_directional_displacement_bps,
    local_lambda,
    measure_event,
    qualifies,
    read_selection,
    reduce_window,
    resolve_direction,
    select_specification,
    session_clustered_inference,
    specification_by_name,
    supporting_count,
    tile_prior_window,
    window_rows,
    write_selection,
)
from app.services.stage41_iag_plan import (
    ABSORPTION_DISQUALIFY_PERCENTILE,
    CERTIFIED_SYMBOLS,
    EFFECTIVE_TRIALS_AFTER_DESIGN,
    EFFECTIVE_TRIALS_AFTER_REVEAL,
    EFFECTIVE_TRIALS_BEFORE,
    EXPECTED_DESIGN_SHA256,
    HIGH_PERCENTILE,
    LAMBDA_MIN_DENOMINATOR_SHARES,
    LONG,
    MIN_AGREEING_QUARTERS,
    MIN_BASELINE_TILES,
    MIN_EVENTS,
    MIN_ROWS_CONFIRMING,
    MIN_ROWS_PRIMARY,
    MIN_SESSIONS,
    NANOS_PER_SECOND,
    OBSERVATION_NS,
    OBSERVATION_SECONDS,
    PERSISTENCE_QUARTERS,
    PRIMARY_GROSS_HURDLE_BPS,
    PRIMARY_HORIZON_MINUTES,
    QUIET_PERIOD_MINUTES,
    SECONDARY_HORIZON_MINUTES,
    SHORT,
    SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION,
    SPEC_FALLBACK,
    SPEC_PRIMARY,
    T_HURDLE,
    UNUSABLE_FEATURES,
    VERDICT_DETECTED,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_MECHANISM,
    assert_directional_use_is_permitted,
    assert_frozen_design,
    impacted_depth_column,
    statistical_plan,
)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]

T0 = 1_748_872_785_000_000_000
T_OBS_END = T0 + OBSERVATION_NS


# ---------------------------------------------------------------------------
# Frozen design
# ---------------------------------------------------------------------------


def test_the_frozen_design_verifies():
    verified = assert_frozen_design(REPO_ROOT)
    assert verified["design"]["sha256"] == EXPECTED_DESIGN_SHA256
    assert len(verified["design_json"]["sha256"]) == 64


def test_a_modified_design_is_refused(tmp_path):
    import shutil

    (tmp_path / "docs").mkdir()
    (tmp_path / "reports" / "tier1_stage41_design" / "v1").mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "docs" / "2026-08-21-stage41-iag-v1-design.md",
        tmp_path / "docs" / "2026-08-21-stage41-iag-v1-design.md",
    )
    shutil.copy2(
        REPO_ROOT / "reports/tier1_stage41_design/v1/stage41_iag_v1_design.json",
        tmp_path / "reports/tier1_stage41_design/v1/stage41_iag_v1_design.json",
    )
    target = tmp_path / "docs" / "2026-08-21-stage41-iag-v1-design.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nsneak\n", encoding="utf-8")
    with pytest.raises(ValueError, match="design has changed"):
        assert_frozen_design(tmp_path)


def test_a_missing_design_is_refused(tmp_path):
    with pytest.raises(ValueError, match="is missing"):
        assert_frozen_design(tmp_path)


def test_the_design_and_its_json_are_both_hashed():
    """The document binds; the JSON is what a program reads. If they could drift
    apart the program would run a specification nobody approved."""
    source = inspect.getsource(assert_frozen_design)
    assert "design_json" in source
    verified = assert_frozen_design(REPO_ROOT)
    assert set(verified) == {"design", "design_json"}


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_design_stage_consumes_no_trial():
    assert EFFECTIVE_TRIALS_BEFORE == 531
    assert EFFECTIVE_TRIALS_AFTER_DESIGN == 531
    plan = statistical_plan()
    assert plan["effective_trials_before"] == plan["effective_trials_after"] == 531


def test_the_reveal_moves_the_ledger_exactly_once():
    assert EFFECTIVE_TRIALS_AFTER_REVEAL == 532
    assert EFFECTIVE_TRIALS_AFTER_REVEAL == EFFECTIVE_TRIALS_BEFORE + 1


def test_the_plan_declares_the_approved_governance_parameters():
    plan = statistical_plan()
    assert plan["observation_window"]["seconds"] == OBSERVATION_SECONDS == 120
    assert plan["economic_test"]["primary_horizon_minutes"] == 15
    assert plan["baseline"]["percentile_levels"] == [25.0, 75.0]
    assert plan["direction"]["cadences_must_agree"] == ["50ev", "200ev"]
    assert plan["direction"]["min_agreeing_quarters"] == 3
    assert plan["direction"]["persistence_quarters"] == 4
    assert plan["population"]["quiet_period_minutes"] == QUIET_PERIOD_MINUTES == 60
    assert plan["sample_floors"] == {"min_events": 100, "min_sessions": 15}


def test_the_hurdle_decomposes_as_declared():
    plan = statistical_plan()
    hurdle = plan["hurdle"]
    assert hurdle["desired_net_bps"] == 8.0
    assert hurdle["execution_allowance_bps"] == 4.0
    assert hurdle["primary_gross_hurdle_bps"] == 12.0
    assert hurdle["desired_net_bps"] + hurdle["execution_allowance_bps"] == 12.0
    assert hurdle["is_expected_return"] is False


def test_no_post_news_shock_threshold_exists():
    """Stage 3.6 selected on the initial move. This deliberately does not."""
    plan = statistical_plan()
    assert plan["population"]["post_news_shock_threshold"] is None


def test_the_certified_population_is_unchanged():
    plan = statistical_plan()
    assert plan["population"]["symbols"] == list(CERTIFIED_SYMBOLS)
    assert len(CERTIFIED_SYMBOLS) == 8
    assert plan["population"]["sessions"] == 20
    assert plan["population"]["candidate_events"] == 502


# ---------------------------------------------------------------------------
# Feature semantics -- the side-agnostic findings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature", SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION)
def test_a_side_agnostic_feature_cannot_be_used_directionally(feature):
    """Each accumulates both book sides into one counter."""
    with pytest.raises(ValueError, match="both book sides"):
        assert_directional_use_is_permitted(feature)


def test_the_forbidden_list_names_the_features_that_look_directional():
    """refill_after_execution_volume is the one that would slip past review: it
    consults the event's side when deciding to increment, then accumulates into
    a single counter."""
    for name in (
        "queue_depletion_events",
        "touch_replenishment_volume",
        "touch_replenishment_events",
        "refill_after_execution_volume",
        "depletion_followed_by_quote_move",
        "cancel_add_ratio",
    ):
        assert name in SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION


@pytest.mark.parametrize("feature", UNUSABLE_FEATURES)
def test_an_identically_zero_feature_is_refused(feature):
    with pytest.raises(ValueError, match="identically zero"):
        assert_directional_use_is_permitted(feature)


@pytest.mark.parametrize(
    "feature", ["signed_trade_volume", "ask_depth_10", "bid_depth_10"]
)
def test_the_directional_features_are_permitted(feature):
    entry = assert_directional_use_is_permitted(feature)
    assert entry.directional is True


def test_a_regime_descriptor_is_refused_as_directional():
    with pytest.raises(ValueError, match="regime descriptor"):
        assert_directional_use_is_permitted("absorption_ratio")


def test_an_undeclared_feature_fails_closed():
    with pytest.raises(ValueError, match="no declared Stage-4.1 semantics"):
        assert_directional_use_is_permitted("some_feature_nobody_declared")


def test_the_impacted_side_follows_the_direction():
    """Buy pressure lifts offers, so asks are consumed; sell pressure hits bids."""
    assert impacted_depth_column(LONG) == "ask_depth_10"
    assert impacted_depth_column(SHORT) == "bid_depth_10"
    with pytest.raises(ValueError, match="neither"):
        impacted_depth_column(0)


# ---------------------------------------------------------------------------
# Window construction helpers
# ---------------------------------------------------------------------------


def _columns(*, count, flow, ask, bid, mid, spread=2.0, absorption=0.1,
             intensity=5.0, cancels=1.0, volume=100.0, start=T0, step_ns=None):
    """A synthetic feature block, one row per step across the window."""
    step = step_ns if step_ns is not None else OBSERVATION_NS // max(count, 1)
    available = np.array([start + i * step for i in range(count)], dtype=np.int64)

    def _spread_array(value):
        return np.full(count, value, dtype=float) if np.isscalar(value) else np.asarray(value, float)

    return {
        "feature_available_ts_recv": available,
        "signed_trade_volume": _spread_array(flow),
        "ask_depth_10": _spread_array(ask),
        "bid_depth_10": _spread_array(bid),
        "midpoint": _spread_array(mid),
        "spread_bps": _spread_array(spread),
        "absorption_ratio": _spread_array(absorption),
        "execution_intensity": _spread_array(intensity),
        "cancel_volume_ratio": _spread_array(cancels),
        "execution_volume": _spread_array(volume),
    }


def _uniform(count, value):
    return np.full(count, float(value))


# ---------------------------------------------------------------------------
# Window reduction
# ---------------------------------------------------------------------------


def test_counters_are_summed_and_snapshots_are_not():
    """The central implementation trap: window counters reset per emission, so
    summing reconstructs the interval total; snapshots are state and summing
    them would be meaningless."""
    columns = _columns(
        count=4,
        flow=[10.0, 20.0, -5.0, 15.0],
        ask=[1000.0, 800.0, 400.0, 600.0],
        bid=_uniform(4, 900.0),
        mid=[100.0, 100.5, 101.0, 101.5],
    )
    stats = reduce_window(columns, window_rows(columns, start_ns=T0, end_ns=T_OBS_END))

    assert stats.net_flow == 40.0  # summed
    assert stats.ask_depth_first == 1000.0  # first, not summed
    assert stats.ask_depth_last == 600.0  # last
    assert stats.ask_depth_min == 400.0  # trough
    assert stats.midpoint_first == 100.0
    assert stats.midpoint_last == 101.5
    assert stats.rows == 4


def test_rows_are_ordered_by_the_availability_clock_not_file_order():
    """An out-of-order file must not change which row counts as first."""
    columns = _columns(count=3, flow=_uniform(3, 1.0), ask=[10.0, 20.0, 30.0],
                       bid=_uniform(3, 5.0), mid=[100.0, 200.0, 300.0])
    # Shuffle the clock so file order and time order disagree.
    columns["feature_available_ts_recv"] = np.array(
        [T0 + 50 * NANOS_PER_SECOND, T0, T0 + 100 * NANOS_PER_SECOND], dtype=np.int64
    )
    stats = reduce_window(columns, window_rows(columns, start_ns=T0, end_ns=T_OBS_END))
    assert stats.midpoint_first == 200.0  # the row stamped T0
    assert stats.midpoint_last == 300.0


def test_the_window_bounds_are_inclusive_at_both_ends():
    columns = _columns(count=3, flow=_uniform(3, 1.0), ask=_uniform(3, 10.0),
                       bid=_uniform(3, 10.0), mid=_uniform(3, 100.0))
    columns["feature_available_ts_recv"] = np.array(
        [T0 - 1, T0, T_OBS_END], dtype=np.int64
    )
    rows = window_rows(columns, start_ns=T0, end_ns=T_OBS_END)
    assert rows.size == 2  # the pre-t0 row is excluded, both boundaries included

    rows = window_rows(columns, start_ns=T0, end_ns=T_OBS_END - 1)
    assert rows.size == 1


def test_a_null_midpoint_does_not_poison_the_endpoints():
    """A one-sided book yields a null midpoint; the finite ones still resolve."""
    columns = _columns(count=4, flow=_uniform(4, 1.0), ask=_uniform(4, 10.0),
                       bid=_uniform(4, 10.0),
                       mid=[np.nan, 100.0, 101.0, np.nan])
    stats = reduce_window(columns, window_rows(columns, start_ns=T0, end_ns=T_OBS_END))
    assert stats.midpoint_first == 100.0
    assert stats.midpoint_last == 101.0


def test_absorption_is_volume_weighted():
    """A quiet window must not outvote a busy one."""
    columns = _columns(count=2, flow=_uniform(2, 1.0), ask=_uniform(2, 10.0),
                       bid=_uniform(2, 10.0), mid=_uniform(2, 100.0),
                       absorption=[0.0, 1.0], volume=[1.0, 99.0])
    stats = reduce_window(columns, window_rows(columns, start_ns=T0, end_ns=T_OBS_END))
    assert stats.absorption_ratio == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def _direction_columns(quarter_flows, *, rows_per_quarter=8):
    """Rows spread evenly across the four quarters with the given net flows."""
    quarter_ns = OBSERVATION_NS // PERSISTENCE_QUARTERS
    available, flow = [], []
    for index, total in enumerate(quarter_flows):
        base = T0 + index * quarter_ns
        for row in range(rows_per_quarter):
            available.append(base + row * (quarter_ns // (rows_per_quarter + 1)))
            flow.append(total / rows_per_quarter)
    count = len(available)
    columns = _columns(count=count, flow=flow, ask=_uniform(count, 500.0),
                       bid=_uniform(count, 500.0), mid=_uniform(count, 100.0))
    columns["feature_available_ts_recv"] = np.array(available, dtype=np.int64)
    return columns


def test_direction_requires_both_cadences_to_agree():
    primary = _direction_columns([100.0, 100.0, 100.0, 100.0])
    confirming = _direction_columns([100.0, 100.0, 100.0, 100.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.direction == LONG
    assert verdict.is_unambiguous


def test_cadence_disagreement_is_ambiguous():
    primary = _direction_columns([100.0, 100.0, 100.0, 100.0])
    confirming = _direction_columns([-100.0, -100.0, -100.0, -100.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.direction is None
    assert verdict.reason == "cadence_disagreement"


def test_zero_net_flow_is_ambiguous():
    primary = _direction_columns([100.0, -100.0, 100.0, -100.0])
    confirming = _direction_columns([100.0, 100.0, 100.0, 100.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.direction is None
    assert verdict.reason == "zero_net_flow"


def test_a_burst_without_persistence_is_ambiguous():
    """Net sign alone cannot distinguish sustained pressure from one large
    print in an otherwise balanced two minutes. Persistence can."""
    primary = _direction_columns([1000.0, -10.0, -10.0, -10.0])
    confirming = _direction_columns([1000.0, -10.0, -10.0, -10.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.net_flow_primary > 0  # net sign says long
    assert verdict.agreeing_quarters == 1
    assert verdict.direction is None  # persistence says no
    assert verdict.reason == "not_persistent"


def test_exactly_three_of_four_quarters_is_enough():
    primary = _direction_columns([100.0, 100.0, -50.0, 100.0])
    confirming = _direction_columns([100.0, 100.0, -50.0, 100.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.agreeing_quarters == MIN_AGREEING_QUARTERS == 3
    assert verdict.direction == LONG


def test_a_silent_quarter_does_not_agree():
    """Silence is not confirmation."""
    primary = _direction_columns([100.0, 100.0, 0.0, 0.0])
    confirming = _direction_columns([100.0, 100.0, 0.0, 0.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.agreeing_quarters == 2
    assert verdict.direction is None


def test_short_direction_resolves_symmetrically():
    primary = _direction_columns([-100.0, -100.0, -100.0, -100.0])
    confirming = _direction_columns([-100.0, -100.0, -100.0, -100.0])
    verdict = resolve_direction(primary, confirming, t0_ns=T0, t_obs_end_ns=T_OBS_END)
    assert verdict.direction == SHORT


def test_direction_never_reads_a_price():
    """Direction comes from order flow, never from the news move or from price."""
    tree = ast.parse(inspect.getsource(resolve_direction))
    names = {n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for banned in ("midpoint", "spread_bps", "ask_depth_10", "bid_depth_10"):
        assert banned not in names


# ---------------------------------------------------------------------------
# Local lambda
# ---------------------------------------------------------------------------


def _stats(**overrides):
    from app.services.stage41_iag_executor import WindowStats

    base = {
        "rows": 50, "net_flow": 1000.0, "first_ts": T0, "last_ts": T_OBS_END,
        "ask_depth_first": 1000.0, "ask_depth_last": 200.0, "ask_depth_min": 100.0,
        "bid_depth_first": 1000.0, "bid_depth_last": 900.0, "bid_depth_min": 800.0,
        "midpoint_first": 100.0, "midpoint_last": 100.1, "spread_bps_last": 3.0,
        "absorption_ratio": 0.1, "execution_intensity": 5.0,
        "cancel_volume_ratio": 1.0,
    }
    base.update(overrides)
    return WindowStats(**base)


def test_lambda_is_bps_per_thousand_shares():
    """10 bps of displacement on 1,000 shares of net flow is 10 bps/1k."""
    value = local_lambda(_stats(midpoint_first=100.0, midpoint_last=100.1,
                                net_flow=1000.0), LONG)
    assert value == pytest.approx(10.0)


def test_lambda_scales_inversely_with_flow():
    """Twice the flow for the same displacement is half the impact per share."""
    small = local_lambda(_stats(net_flow=1000.0), LONG)
    large = local_lambda(_stats(net_flow=2000.0), LONG)
    assert large == pytest.approx(small / 2)


def test_lambda_incorporates_direction_in_both_terms():
    """A short whose price fell has positive impact, same as a long that rose."""
    long_value = local_lambda(
        _stats(midpoint_first=100.0, midpoint_last=100.1, net_flow=1000.0), LONG
    )
    short_value = local_lambda(
        _stats(midpoint_first=100.0, midpoint_last=99.9, net_flow=-1000.0), SHORT
    )
    assert long_value == pytest.approx(short_value)
    assert long_value > 0


def test_lambda_is_negative_when_price_moved_against_the_flow():
    value = local_lambda(
        _stats(midpoint_first=100.0, midpoint_last=99.9, net_flow=1000.0), LONG
    )
    assert value < 0


def test_lambda_fails_closed_below_the_minimum_flow():
    assert LAMBDA_MIN_DENOMINATOR_SHARES == 100
    assert local_lambda(_stats(net_flow=99.0), LONG) is None
    assert local_lambda(_stats(net_flow=100.0), LONG) is not None


def test_lambda_fails_closed_on_zero_flow():
    assert local_lambda(_stats(net_flow=0.0), LONG) is None


def test_lambda_fails_closed_when_flow_opposes_the_direction():
    """A long direction with net selling gives a negative denominator, which is
    not a measurable impact per unit of same-direction flow."""
    assert local_lambda(_stats(net_flow=-5000.0), LONG) is None


@pytest.mark.parametrize("field", ["midpoint_first", "midpoint_last"])
def test_lambda_fails_closed_on_a_missing_endpoint(field):
    """A one-sided or empty book at either end leaves the endpoint unknown."""
    assert local_lambda(_stats(**{field: None}), LONG) is None


def test_lambda_fails_closed_on_a_non_positive_start_midpoint():
    assert local_lambda(_stats(midpoint_first=0.0), LONG) is None


def test_lambda_is_not_winsorized():
    """Rank thresholds are robust by construction; clipping would add a knob
    that could be tuned."""
    from app.services.stage41_iag_plan import LAMBDA_WINSORIZATION

    assert LAMBDA_WINSORIZATION is None
    extreme = local_lambda(_stats(midpoint_last=200.0, net_flow=100.0), LONG)
    assert extreme > 1000  # passed through unclipped


def test_lambda_reads_only_window_statistics():
    """Its inputs are a reduced window and a direction. There is no clock
    argument through which a later instant could enter."""
    signature = inspect.signature(local_lambda)
    assert list(signature.parameters) == ["stats", "direction"]


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def _tile_stats(n, **overrides):
    return [_stats(**{k: (v[i] if isinstance(v, list) else v)
                      for k, v in overrides.items()}) for i in range(n)]


def test_the_baseline_percentile_locates_a_value_in_the_prior_distribution():
    tiles = [_stats(ask_depth_last=float(i)) for i in range(100)]
    baseline = build_baseline("AAPL", tiles)
    assert baseline.percentile_of("ask_depth_last", -1.0) == pytest.approx(0.0)
    assert baseline.percentile_of("ask_depth_last", 1000.0) == pytest.approx(100.0)
    assert baseline.percentile_of("ask_depth_last", 49.5) == pytest.approx(50.0)


def test_the_baseline_refuses_a_value_it_cannot_place():
    baseline = build_baseline("AAPL", [_stats()])
    assert baseline.percentile_of("ask_depth_last", None) is None
    assert baseline.percentile_of("a_statistic_that_does_not_exist", 1.0) is None
    assert baseline.percentile_of("ask_depth_last", float("nan")) is None


def test_a_thin_baseline_is_insufficient():
    assert MIN_BASELINE_TILES == 500
    assert not build_baseline("AAPL", [_stats()] * 499).is_sufficient
    assert build_baseline("AAPL", [_stats()] * 500).is_sufficient


def test_the_baseline_holds_separate_long_and_short_lambda_samples():
    """Lambda's sign depends on direction, so one pooled sample would score a
    long against a distribution half built from shorts."""
    baseline = build_baseline("AAPL", [_stats()] * 10)
    assert "lambda_long" in baseline.samples
    assert "lambda_short" in baseline.samples


def test_tiling_produces_non_overlapping_windows():
    rows = 400
    columns = _columns(
        count=rows, flow=_uniform(rows, 1.0), ask=_uniform(rows, 500.0),
        bid=_uniform(rows, 500.0), mid=_uniform(rows, 100.0),
        step_ns=NANOS_PER_SECOND,
    )
    tiles = tile_prior_window(columns, start_ns=T0, end_ns=T0 + 400 * NANOS_PER_SECOND)
    # 400 seconds of rows at 120s per tile = 3 complete tiles.
    assert len(tiles) == 3
    starts = [t.first_ts for t in tiles]
    assert starts == sorted(starts)
    for earlier, later in itertools.pairwise(tiles):
        assert earlier.last_ts < later.first_ts


def test_a_sparse_tile_is_dropped_from_the_baseline():
    """The baseline must describe windows comparable to the one being scored."""
    columns = _columns(
        count=5, flow=_uniform(5, 1.0), ask=_uniform(5, 500.0),
        bid=_uniform(5, 500.0), mid=_uniform(5, 100.0), step_ns=NANOS_PER_SECOND,
    )
    tiles = tile_prior_window(columns, start_ns=T0, end_ns=T0 + 240 * NANOS_PER_SECOND)
    assert tiles == []  # 5 rows is below MIN_ROWS_PRIMARY


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


def _state(**overrides):
    base = {
        "symbol": "AAPL", "session_date": "2025-06-02", "story_id": "abc123",
        "t0_ns": T0, "t_obs_end_ns": T_OBS_END,
        "rows_primary": 90, "rows_confirming": 350,
        "direction": LONG, "direction_reason": None, "agreeing_quarters": 4,
        "net_flow_primary": 5000.0, "net_flow_confirming": 5000.0,
        "depth_percentile": 10.0, "recovery": 0.10, "absorption_percentile": 30.0,
        "lambda_value": 8.0, "lambda_percentile": 80.0, "spread_percentile": 80.0,
        "intensity_percentile": 40.0, "cancel_percentile": 40.0,
        "baseline_tiles": 1000,
    }
    base.update(overrides)
    return EventState(**base)


def test_a_fully_qualifying_event_passes():
    ok, reason, support = qualifies(_state(), SPEC_PRIMARY)
    assert ok is True
    assert reason is None
    assert support == 2


def test_a_thin_window_is_refused_first():
    ok, reason, _ = qualifies(_state(rows_primary=MIN_ROWS_PRIMARY - 1), SPEC_PRIMARY)
    assert (ok, reason) == (False, FAIL_THIN_WINDOW)
    ok, reason, _ = qualifies(
        _state(rows_confirming=MIN_ROWS_CONFIRMING - 1), SPEC_PRIMARY
    )
    assert (ok, reason) == (False, FAIL_THIN_WINDOW)


def test_a_thin_baseline_is_refused():
    ok, reason, _ = qualifies(_state(baseline_tiles=499), SPEC_PRIMARY)
    assert (ok, reason) == (False, FAIL_THIN_BASELINE)


def test_an_ambiguous_direction_is_refused():
    ok, reason, _ = qualifies(_state(direction=None), SPEC_PRIMARY)
    assert (ok, reason) == (False, FAIL_AMBIGUOUS_DIRECTION)


def test_an_undepleted_impacted_side_is_refused():
    ok, reason, _ = qualifies(_state(depth_percentile=26.0), SPEC_PRIMARY)
    assert (ok, reason) == (False, FAIL_NO_DEPLETION)
    # Exactly at the threshold qualifies.
    assert qualifies(_state(depth_percentile=25.0), SPEC_PRIMARY)[0] is True


def test_a_replenished_side_is_refused():
    ok, reason, _ = qualifies(_state(recovery=0.26), SPEC_PRIMARY)
    assert (ok, reason) == (False, FAIL_REPLENISHED)
    assert qualifies(_state(recovery=0.25), SPEC_PRIMARY)[0] is True


def test_an_absorbing_market_is_refused():
    """High absorption means executions left the midpoint unchanged -- the
    market IS absorbing, which is the opposite of an assimilation gap."""
    ok, reason, _ = qualifies(
        _state(absorption_percentile=ABSORPTION_DISQUALIFY_PERCENTILE), SPEC_PRIMARY
    )
    assert (ok, reason) == (False, FAIL_ABSORBED)
    assert qualifies(_state(absorption_percentile=74.9), SPEC_PRIMARY)[0] is True


def test_an_unmeasurable_absorption_does_not_disqualify():
    """We could not measure it is not evidence that it was high."""
    assert qualifies(_state(absorption_percentile=None), SPEC_PRIMARY)[0] is True


def test_too_few_supporting_conditions_is_refused():
    ok, reason, support = qualifies(
        _state(lambda_percentile=10.0, spread_percentile=10.0), SPEC_PRIMARY
    )
    assert (ok, reason) == (False, FAIL_SUPPORT)
    assert support == 0


def test_supporting_conditions_count_at_the_threshold():
    assert supporting_count(_state(
        lambda_percentile=HIGH_PERCENTILE, spread_percentile=74.9,
        intensity_percentile=None, cancel_percentile=None)) == 1


def test_an_undefined_lambda_does_not_satisfy_its_condition_but_does_not_disqualify():
    """S3 unsatisfied; the event can still qualify on the other three."""
    state = _state(lambda_value=None, lambda_percentile=None,
                   spread_percentile=80.0, intensity_percentile=80.0)
    assert supporting_count(state) == 2
    assert qualifies(state, SPEC_PRIMARY)[0] is True


def test_lambda_alone_can_never_qualify_an_event():
    """It conditions on in-window displacement, so it is supporting only --
    two conditions are always required."""
    state = _state(lambda_percentile=99.0, spread_percentile=10.0,
                   intensity_percentile=10.0, cancel_percentile=10.0)
    assert supporting_count(state) == 1
    assert qualifies(state, SPEC_PRIMARY)[0] is False


def test_the_fallback_is_strictly_looser_on_the_two_declared_dimensions():
    assert SPEC_FALLBACK.depletion_percentile > SPEC_PRIMARY.depletion_percentile
    assert SPEC_FALLBACK.recovery_threshold > SPEC_PRIMARY.recovery_threshold
    assert SPEC_FALLBACK.min_supporting == SPEC_PRIMARY.min_supporting == 2

    marginal = _state(depth_percentile=40.0, recovery=0.40)
    assert qualifies(marginal, SPEC_PRIMARY)[0] is False
    assert qualifies(marginal, SPEC_FALLBACK)[0] is True


def test_the_absorption_disqualifier_does_not_vary_between_specifications():
    """It is not a supply knob -- it is the statement that an absorbing market
    is not an assimilation gap."""
    absorbing = _state(absorption_percentile=90.0)
    assert qualifies(absorbing, SPEC_PRIMARY)[1] == FAIL_ABSORBED
    assert qualifies(absorbing, SPEC_FALLBACK)[1] == FAIL_ABSORBED


def test_every_failure_reason_is_in_the_declared_vocabulary():
    for state in (
        _state(rows_primary=1), _state(baseline_tiles=1), _state(direction=None),
        _state(depth_percentile=99.0), _state(recovery=0.99),
        _state(absorption_percentile=99.0), _state(lambda_percentile=1.0,
                                                   spread_percentile=1.0),
    ):
        _ok, reason, _support = qualifies(state, SPEC_PRIMARY)
        assert reason in FAILURE_REASONS


# ---------------------------------------------------------------------------
# Specification selection -- counts only
# ---------------------------------------------------------------------------


def _population(*, qualifying_primary, qualifying_fallback_only, sessions):
    """A population with a controlled number of events under each spec."""
    states = []
    for index in range(qualifying_primary):
        states.append(_state(
            story_id=f"p{index}", session_date=f"2025-06-{(index % sessions) + 2:02d}",
            depth_percentile=10.0, recovery=0.10))
    for index in range(qualifying_fallback_only):
        states.append(_state(
            story_id=f"f{index}", session_date=f"2025-06-{(index % sessions) + 2:02d}",
            depth_percentile=40.0, recovery=0.40))
    return states


def test_primary_is_selected_when_it_clears_both_floors():
    states = _population(qualifying_primary=120, qualifying_fallback_only=50,
                         sessions=18)
    selection = select_specification(states)
    assert selection["selected_specification"] == SPEC_PRIMARY.name
    assert selection["fallback_evaluated"] is False
    assert selection["fallback"] is None
    assert selection["economic_run_authorized"] is True


def test_the_fallback_is_not_even_counted_when_primary_passes():
    """Evaluating both would leave two specifications' counts on the record and
    invite a choice between them."""
    states = _population(qualifying_primary=150, qualifying_fallback_only=200,
                         sessions=20)
    selection = select_specification(states)
    assert selection["fallback"] is None


def test_the_fallback_is_selected_only_when_primary_misses_a_floor():
    states = _population(qualifying_primary=40, qualifying_fallback_only=100,
                         sessions=18)
    selection = select_specification(states)
    assert selection["selected_specification"] == SPEC_FALLBACK.name
    assert selection["fallback_evaluated"] is True
    assert selection["primary"]["eligible_events"] == 40
    assert selection["primary"]["clears_floors"] is False
    assert selection["economic_run_authorized"] is True


def test_a_session_shortfall_alone_sends_primary_to_the_fallback():
    """Both floors bind, not just the event count."""
    states = _population(qualifying_primary=200, qualifying_fallback_only=0,
                         sessions=10)
    selection = select_specification(states)
    assert selection["primary"]["eligible_events"] == 200
    assert selection["primary"]["distinct_sessions"] == 10
    assert selection["primary"]["clears_floors"] is False


def test_neither_specification_clearing_means_no_economic_run():
    states = _population(qualifying_primary=10, qualifying_fallback_only=10,
                         sessions=5)
    selection = select_specification(states)
    assert selection["selected_specification"] is None
    assert selection["economic_run_authorized"] is False
    assert selection["verdict_if_no_run"] == VERDICT_INSUFFICIENT


def test_the_selection_ladder_reads_no_outcome():
    """Structural: nothing in the ladder touches a post-decision quantity."""
    source = inspect.getsource(select_specification)
    tree = ast.parse(source)
    called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "gross_directional_displacement_bps" not in called
    assert "session_clustered_inference" not in called
    assert "decide_verdict" not in called
    assert "count_specification" in called


def test_the_counts_report_the_failure_distribution():
    """The refusal distribution is the most informative outcome-blind output
    this stage produces."""
    states = _population(qualifying_primary=5, qualifying_fallback_only=5, sessions=3)
    counts = count_specification(states, SPEC_PRIMARY)
    assert counts.events == 5
    assert sum(counts.failures.values()) == 5
    assert counts.failures[FAIL_NO_DEPLETION] == 5


def test_the_floors_are_the_stage_40_floors_unchanged():
    assert MIN_EVENTS == 100
    assert MIN_SESSIONS == 15


# ---------------------------------------------------------------------------
# Selection persistence
# ---------------------------------------------------------------------------


def test_the_selection_is_persisted_and_hashed(tmp_path):
    record = {"selected_specification": SPEC_PRIMARY.name, "design_sha256": "x" * 64}
    path = tmp_path / "selection.json"
    digest = write_selection(record, path)
    assert len(digest) == 64
    assert read_selection(path, expected_sha256=digest) == record


def test_an_edited_selection_is_refused(tmp_path):
    path = tmp_path / "selection.json"
    digest = write_selection({"selected_specification": SPEC_PRIMARY.name}, path)
    path.write_text(json.dumps({"selected_specification": SPEC_FALLBACK.name}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="selection has changed"):
        read_selection(path, expected_sha256=digest)


def test_a_missing_selection_is_refused(tmp_path):
    with pytest.raises(ValueError, match="Run diagnose first"):
        read_selection(tmp_path / "absent.json")


def test_only_declared_specifications_resolve_by_name():
    assert specification_by_name(SPEC_PRIMARY.name) is SPEC_PRIMARY
    assert specification_by_name(SPEC_FALLBACK.name) is SPEC_FALLBACK
    with pytest.raises(ValueError, match="not a declared"):
        specification_by_name("IAG_v1_TUNED")


# ---------------------------------------------------------------------------
# The economic reveal
# ---------------------------------------------------------------------------


def test_gross_displacement_is_direction_times_midpoint_move():
    value = gross_directional_displacement_bps(
        direction=LONG, midpoint_at_decision=100.0, midpoint_at_horizon=100.12
    )
    assert value == pytest.approx(12.0)


def test_a_short_profits_from_a_falling_midpoint():
    value = gross_directional_displacement_bps(
        direction=SHORT, midpoint_at_decision=100.0, midpoint_at_horizon=99.88
    )
    assert value == pytest.approx(12.0)


def test_the_mechanism_can_be_negative():
    value = gross_directional_displacement_bps(
        direction=LONG, midpoint_at_decision=100.0, midpoint_at_horizon=99.5
    )
    assert value == pytest.approx(-50.0)


def test_a_non_positive_decision_midpoint_is_refused():
    with pytest.raises(ValueError, match="not positive"):
        gross_directional_displacement_bps(
            direction=LONG, midpoint_at_decision=0.0, midpoint_at_horizon=100.0
        )


def test_the_reveal_refuses_an_undirected_event():
    with pytest.raises(ValueError, match="neither"):
        gross_directional_displacement_bps(
            direction=0, midpoint_at_decision=100.0, midpoint_at_horizon=100.0
        )


def test_the_reveal_is_the_only_function_reading_past_the_cutoff():
    """Every other function takes its inputs from the observation window. This
    one takes two midpoints and does not know what a window is."""
    signature = inspect.signature(gross_directional_displacement_bps)
    assert set(signature.parameters) == {
        "direction", "midpoint_at_decision", "midpoint_at_horizon"
    }


def test_the_qualification_path_never_calls_the_reveal():
    """Structural, and the central governance claim: qualification and the
    economic outcome cannot touch."""
    from app.services import stage41_iag_executor as executor

    for function in (
        executor.measure_event, executor.qualifies, executor.supporting_count,
        executor.resolve_direction, executor.local_lambda,
        executor.select_specification, executor.count_specification,
        executor.build_baseline, executor.tile_prior_window,
        executor.reduce_window,
    ):
        tree = ast.parse(inspect.getsource(function))
        called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "gross_directional_displacement_bps" not in called, function.__name__


# ---------------------------------------------------------------------------
# Inference and verdict
# ---------------------------------------------------------------------------


def test_clustered_inference_reports_the_declared_fields():
    values = [10.0, 12.0, 14.0, 11.0, 13.0, 15.0]
    sessions = ["d1", "d1", "d2", "d2", "d3", "d3"]
    result = session_clustered_inference(values, sessions)
    assert result["events"] == 6
    assert result["distinct_sessions"] == 3
    assert result["mean_gross_bps"] == pytest.approx(12.5)
    assert result["median_gross_bps"] == pytest.approx(12.5)
    assert result["clustering"] == "trading_session"
    assert result["ci95_low_bps"] < result["mean_gross_bps"] < result["ci95_high_bps"]


def test_inference_refuses_misaligned_inputs():
    with pytest.raises(ValueError, match="align one-to-one"):
        session_clustered_inference([1.0, 2.0], ["d1"])
    with pytest.raises(ValueError, match="no displacements"):
        session_clustered_inference([], [])


def test_the_verdict_requires_the_hurdle_and_the_t_together():
    passing = {"events": 150, "distinct_sessions": 18,
               "mean_gross_bps": 14.0, "session_clustered_t": 4.0}
    assert decide_verdict(passing)["verdict"] == VERDICT_DETECTED

    small = {**passing, "mean_gross_bps": 11.9}
    assert decide_verdict(small)["verdict"] == VERDICT_NO_MECHANISM

    noisy = {**passing, "session_clustered_t": 2.9}
    assert decide_verdict(noisy)["verdict"] == VERDICT_NO_MECHANISM


def test_the_verdict_boundary_is_inclusive():
    boundary = {"events": 100, "distinct_sessions": 15,
                "mean_gross_bps": PRIMARY_GROSS_HURDLE_BPS,
                "session_clustered_t": T_HURDLE}
    assert decide_verdict(boundary)["verdict"] == VERDICT_DETECTED


def test_an_undersized_sample_outranks_the_hurdle():
    """A mechanism nobody can evaluate is not a mechanism, however large."""
    huge = {"events": 30, "distinct_sessions": 5,
            "mean_gross_bps": 90.0, "session_clustered_t": 12.0}
    assert decide_verdict(huge)["verdict"] == VERDICT_INSUFFICIENT


def test_passing_authorizes_only_the_execution_simulation():
    passing = {"events": 150, "distinct_sessions": 18,
               "mean_gross_bps": 14.0, "session_clustered_t": 4.0}
    assert decide_verdict(passing)["authorizes"] == "stage_4_3_execution_simulation_only"
    failing = {**passing, "mean_gross_bps": 1.0}
    assert decide_verdict(failing)["authorizes"] is None


# ---------------------------------------------------------------------------
# CLI structure and outcome blindness
# ---------------------------------------------------------------------------


def test_diagnose_never_imports_the_reveal():
    """The strongest available guarantee: diagnose cannot compute an outcome
    because the function is not in scope."""
    from app.cli import stage41_iag as cli

    tree = ast.parse(inspect.getsource(cli.diagnose))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
    assert "gross_directional_displacement_bps" not in imported
    assert "session_clustered_inference" not in imported
    assert "decide_verdict" not in imported
    assert "select_specification" in imported


def test_only_run_imports_the_reveal():
    from app.cli import stage41_iag as cli

    tree = ast.parse(inspect.getsource(cli.run))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
    assert "gross_directional_displacement_bps" in imported


def test_the_run_is_gated_by_an_explicit_flag():
    import argparse as _argparse

    from app.cli.stage41_iag import run

    args = _argparse.Namespace(
        i_have_reviewed_the_design=False, output_dir=".", features_dir=".",
        selection_sha256=None, command="run",
    )
    with pytest.raises(ValueError, match="not authorized"):
        run(args)


def test_the_run_has_no_limit_flag():
    """A subset of the frozen specification is a different specification."""
    from app.cli.stage41_iag import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--features-dir", "/tmp/x", "--limit", "10"])


def test_the_parser_exposes_exactly_the_declared_commands():
    from app.cli.stage41_iag import build_parser

    parser = build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, _argparse_subparsers_type()):
            choices |= set(action.choices)
    assert choices == {"plan", "semantics", "diagnose", "run"}


def _argparse_subparsers_type():
    import argparse

    return argparse._SubParsersAction


def test_the_outcome_filter_removes_displacement_keys():
    from app.cli.stage41_iag import _strip_outcomes

    payload = {
        "eligible_events": 150,
        "mean_gross_bps": 14.0,
        "nested": {"session_clustered_t": 4.0, "rows": 10},
        "records": [{"displacement_bps": 3.0, "symbol": "AAPL"}],
    }
    assert _strip_outcomes(payload) == {
        "eligible_events": 150,
        "nested": {"rows": 10},
        "records": [{"symbol": "AAPL"}],
    }


def test_the_filter_keeps_the_declared_hurdle_and_governance_flags():
    """The hurdle is a predeclared requirement, not a measurement, and the
    governance flags assert the absence of the very things being filtered."""
    from app.cli.stage41_iag import _strip_outcomes

    payload = {
        "primary_gross_hurdle_bps": 12.0,
        "contains_post_decision_return": False,
        "contains_pnl": False,
    }
    assert _strip_outcomes(payload) == payload


def test_the_diagnostic_declares_itself_blind():
    from app.cli.stage41_iag import _governance

    block = _governance(revealed=False)
    assert block["contains_strategy_outcome"] is False
    assert block["contains_post_decision_return"] is False
    assert block["contains_pnl"] is False
    assert block["effective_trials_after"] == 531


def test_the_reveal_declares_the_ledger_move():
    from app.cli.stage41_iag import _governance

    block = _governance(revealed=True)
    assert block["contains_post_decision_return"] is True
    assert block["effective_trials_before"] == 531
    assert block["effective_trials_after"] == 532
    assert block["authorizes_paper_or_live"] is False


def test_no_broker_client_is_reachable():
    """Structural, across all three modules, transitively by import name."""
    from app.cli import stage41_iag as cli
    from app.services import stage41_iag_executor as executor
    from app.services import stage41_iag_plan as plan_module

    banned = ("alpaca", "broker", "tradeapi", "ib_insync", "ccxt")
    for module in (plan_module, executor, cli):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for token in banned:
                    assert token not in name.lower(), f"{module.__name__}: {name}"
            if isinstance(node, ast.Call):
                target = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
                assert "submit_order" not in str(target)
                assert "place_order" not in str(target)


def test_the_secondary_horizons_are_declared_and_cannot_rescue_the_primary():
    plan = statistical_plan()
    assert plan["economic_test"]["primary_horizon_minutes"] == PRIMARY_HORIZON_MINUTES
    assert plan["economic_test"]["secondary_diagnostic_horizons"] == list(
        SECONDARY_HORIZON_MINUTES
    )
    assert plan["economic_test"]["secondary_may_rescue_primary"] is False
    # The verdict function only ever sees one inference payload.
    assert list(inspect.signature(decide_verdict).parameters) == ["inference"]


def test_the_outcome_is_not_called_abnormal():
    """No benchmark-adjustment formula is frozen, so there is no baseline
    against which anything here could be abnormal."""
    plan = statistical_plan()
    assert plan["economic_test"]["outcome_name"] == (
        "gross_directional_midpoint_displacement_bps"
    )
    assert plan["economic_test"]["is_pnl"] is False
    source = inspect.getsource(gross_directional_displacement_bps)
    assert "not** P&L" in source or "not P&L" in source


# ---------------------------------------------------------------------------
# End to end on synthetic features
# ---------------------------------------------------------------------------


def test_a_constructed_iag_event_measures_and_qualifies():
    """Persistent buying, ask depth collapsing and not recovering, price
    drifting up: the state the mechanism describes."""
    count = 80
    step = OBSERVATION_NS // count
    ask = np.linspace(1000.0, 150.0, count)  # consumed, never refilled
    columns = _columns(
        count=count, flow=_uniform(count, 60.0), ask=ask,
        bid=_uniform(count, 900.0), mid=np.linspace(100.0, 100.05, count),
        spread=6.0, absorption=0.05, intensity=20.0, cancels=3.0, step_ns=step,
    )
    confirming = _columns(
        count=count * 4, flow=_uniform(count * 4, 15.0),
        ask=_uniform(count * 4, 500.0), bid=_uniform(count * 4, 900.0),
        mid=_uniform(count * 4, 100.0), step_ns=OBSERVATION_NS // (count * 4),
    )

    # A baseline where this window looks extreme: depth low, spread and
    # intensity high.
    tiles = [
        _stats(ask_depth_last=1000.0 + i, spread_bps_last=1.0,
               execution_intensity=1.0, cancel_volume_ratio=0.5,
               absorption_ratio=0.5, midpoint_first=100.0, midpoint_last=100.0,
               net_flow=1000.0)
        for i in range(600)
    ]
    baseline = build_baseline("AAPL", tiles)

    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s1", t0_ns=T0,
        primary=columns, confirming=confirming, baseline=baseline,
    )

    assert state.direction == LONG
    assert state.agreeing_quarters == PERSISTENCE_QUARTERS
    assert state.depth_percentile == pytest.approx(0.0)  # below every prior tile
    assert state.recovery == pytest.approx(0.0)  # ended at its trough
    assert state.spread_percentile == pytest.approx(100.0)
    assert state.intensity_percentile == pytest.approx(100.0)

    ok, reason, support = qualifies(state, SPEC_PRIMARY)
    assert ok is True, reason
    assert support >= 2


def test_a_replenishing_event_does_not_qualify():
    """Same depletion, but the book comes back -- no assimilation gap."""
    count = 80
    step = OBSERVATION_NS // count
    ask = np.concatenate([np.linspace(1000.0, 150.0, count // 2),
                          np.linspace(150.0, 990.0, count - count // 2)])
    columns = _columns(
        count=count, flow=_uniform(count, 60.0), ask=ask,
        bid=_uniform(count, 900.0), mid=np.linspace(100.0, 100.05, count),
        step_ns=step,
    )
    confirming = _columns(
        count=count * 4, flow=_uniform(count * 4, 15.0),
        ask=_uniform(count * 4, 500.0), bid=_uniform(count * 4, 900.0),
        mid=_uniform(count * 4, 100.0), step_ns=OBSERVATION_NS // (count * 4),
    )
    baseline = build_baseline("AAPL", [_stats(ask_depth_last=1000.0 + i)
                                       for i in range(600)])
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s2", t0_ns=T0,
        primary=columns, confirming=confirming, baseline=baseline,
    )
    assert state.recovery > SPEC_PRIMARY.recovery_threshold
    assert qualifies(state, SPEC_PRIMARY)[1] == FAIL_REPLENISHED


def test_measurement_ignores_rows_outside_the_observation_window():
    """Rows after t_obs_end exist in the file and must not be read."""
    count = 160
    columns = _columns(
        count=count, flow=_uniform(count, 50.0),
        # 81 rows fall inside the window: indices 0..80, since the row stamped
        # exactly t_obs_end is included. The contaminated block therefore has to
        # start at 81, not 80.
        ask=np.concatenate([np.linspace(1000.0, 200.0, 81), _uniform(79, 5.0)]),
        bid=_uniform(count, 900.0),
        mid=np.concatenate([np.linspace(100.0, 100.05, 81), _uniform(79, 999.0)]),
        step_ns=OBSERVATION_NS // 80,
    )
    confirming = _columns(
        count=count * 4, flow=_uniform(count * 4, 12.0),
        ask=_uniform(count * 4, 500.0), bid=_uniform(count * 4, 900.0),
        mid=_uniform(count * 4, 100.0), step_ns=OBSERVATION_NS // (80 * 4),
    )
    baseline = build_baseline("AAPL", [_stats()] * 600)
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s3", t0_ns=T0,
        primary=columns, confirming=confirming, baseline=baseline,
    )
    # The row stamped exactly t_obs_end is inside the window, so 81 rows count.
    assert state.rows_primary == 81
    assert state.lambda_value is not None
    # Had any 999.0 midpoint entered, the displacement would be ~+89,900 bps.
    # The clean window moves 100.00 -> 100.05, which is 5 bps.
    clean_bps = (100.05 - 100.0) / 100.0 * 10_000
    contaminated_bps = (999.0 - 100.0) / 100.0 * 10_000
    observed_bps = state.lambda_value * (50.0 * 81) / 1000
    assert observed_bps == pytest.approx(clean_bps, rel=1e-6)
    assert observed_bps < contaminated_bps / 1000
