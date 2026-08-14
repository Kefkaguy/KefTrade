from __future__ import annotations

import os
import random
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.cli.intraday_alpha_map import parser
from app.services.intraday_factor_diagnostics import load_cost_model, sector_map
from app.services.intraday_alpha_map import (
    DEFAULT_COST_SAFETY_MULTIPLE,
    TRANSFORMS,
    VERDICT_BELOW_COST,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_INFORMATION,
    VERDICT_TRADABLE,
    CellKey,
    attach_forward_returns,
    bucket_profile,
    cost_hurdle,
    cross_sectional_dependence,
    declare_alpha_map,
    declared_cell_keys,
    expanding_normalization,
    forward_return_ladder,
    horizon_availability,
    horizon_cost_feasibility,
    load_alpha_map_panel,
    measure_alpha_map,
    measure_cell,
    monotonicity,
    probability_of_backtest_overfitting,
    ranks,
    residualize_cross_section,
    run_alpha_map,
    session_clustered_ic,
    spearman,
)
from app.services.research_splits import get_dataset_splits


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def test_ranks_average_ties_rather_than_inventing_an_order():
    assert ranks([3.0, 1.0, 2.0, 2.0]) == [4.0, 1.0, 2.5, 2.5]


def test_spearman_is_one_for_a_monotone_transform():
    assert spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_refuses_a_constant_series_instead_of_dividing_by_zero():
    assert spearman([1, 2, 3, 4], [7, 7, 7, 7]) is None


# ---------------------------------------------------------------------------
# Horizon ladder
# ---------------------------------------------------------------------------


def test_sub_grid_horizons_are_refused_by_name_not_rounded_up():
    report = horizon_availability([10, 30, 60, 90, 300], grid_seconds=60)
    assert report["available_seconds"] == [60, 300]
    assert "below_measurement_grid" in report["unavailable"]["10s"]
    assert "below_measurement_grid" in report["unavailable"]["30s"]
    assert "not_on_measurement_grid" in report["unavailable"]["90s"]


def _grid(bars: int = 40, *, start_minute: int = 0) -> list[dict]:
    return [
        {
            "timestamp": datetime(2026, 3, 4, 15, start_minute, tzinfo=UTC)
            + timedelta(minutes=index),
            "open": 100 + index * 0.01,
            "high": 100 + index * 0.01 + 0.02,
            "low": 100 + index * 0.01 - 0.02,
            "close": 100 + index * 0.01 + 0.005,
        }
        for index in range(bars)
    ]


def test_forward_returns_enter_at_the_next_bar_open_not_the_signal_close():
    ladder = forward_return_ladder(
        _grid(),
        decision_timestamp=datetime(2026, 3, 4, 15, 5, tzinfo=UTC),
        horizons_seconds=(60, 300),
        grid_seconds=60,
    )
    # The bar opening exactly at the decision instant is the first tradable
    # price; the signal bar's own close is not a price anyone could have hit.
    assert ladder[60]["entry_price"] == pytest.approx(100.05)
    assert ladder[300]["bars"] == 5
    assert ladder[300]["gross_return_bps"] > ladder[60]["gross_return_bps"]


def test_a_horizon_running_past_the_session_is_dropped_not_shortened():
    ladder = forward_return_ladder(
        _grid(bars=10),
        decision_timestamp=datetime(2026, 3, 4, 15, 5, tzinfo=UTC),
        horizons_seconds=(3_600,),
        grid_seconds=60,
    )
    assert ladder[3_600] == {"available": False, "reason": "session_end_before_horizon"}


def test_a_gap_in_the_grid_is_detected_rather_than_measured_as_a_longer_forecast():
    rows = _grid()
    gapped = rows[:10] + rows[15:]
    ladder = forward_return_ladder(
        gapped,
        decision_timestamp=datetime(2026, 3, 4, 15, 5, tzinfo=UTC),
        horizons_seconds=(600,),
        grid_seconds=60,
    )
    assert ladder[600] == {"available": False, "reason": "gap_in_measurement_grid"}


# ---------------------------------------------------------------------------
# Normalization and residualization
# ---------------------------------------------------------------------------


def _observation(symbol: str, timestamp: datetime, value: float, slot: str = "10:00") -> dict:
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "session_date": timestamp.date(),
        "slot": slot,
        "minutes_from_open": 30,
        "features": {"flow": value},
        "cost_bps": 2.0,
    }


def test_normalization_uses_only_prior_history_for_the_same_symbol_and_slot():
    base = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    rows = [_observation("AAA", base + timedelta(days=index), 1.0) for index in range(25)]
    # A single large value at the end must not affect anything before it.
    rows.append(_observation("AAA", base + timedelta(days=25), 9.0))
    expanding_normalization(rows, feature="flow", minimum_history=20)

    assert rows[10]["transforms"]["flow::zscore_symbol_tod"] is None  # too little history
    assert rows[22]["transforms"]["flow::zscore_symbol_tod"] == pytest.approx(0.0)
    # The outlier is scored against a constant history, so its percentile is
    # the top of that history rather than a full-sample statistic.
    assert rows[-1]["transforms"]["flow::percentile_symbol_tod"] == pytest.approx(1.0)


def test_normalization_is_per_symbol_so_two_stocks_do_not_share_a_threshold():
    base = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    rows: list[dict] = []
    for index in range(25):
        moment = base + timedelta(days=index)
        rows.append(_observation("CALM", moment, 0.01 * index))
        rows.append(_observation("WILD", moment, 1.00 * index))
    rows.append(_observation("CALM", base + timedelta(days=25), 0.30))
    rows.append(_observation("WILD", base + timedelta(days=25), 0.30))
    expanding_normalization(rows, feature="flow", minimum_history=20)

    calm = rows[-2]["transforms"]["flow::zscore_symbol_tod"]
    wild = rows[-1]["transforms"]["flow::zscore_symbol_tod"]
    # Identical raw values, opposite meanings. An absolute threshold cannot see
    # this and would fire on both or neither.
    assert calm > 1.5
    assert wild < -1.0


def test_residualization_separates_a_market_wide_move_from_a_stock_specific_one():
    moment = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    rows = [_observation(symbol, moment, -0.30) for symbol in ("AAA", "BBB", "CCC", "DDD")]
    rows.append(_observation("EEE", moment, -0.90))
    summary = residualize_cross_section(rows, feature="flow", sectors={})

    # Four symbols moving together carry no idiosyncratic signal; the fifth,
    # which moved three times as far, keeps a residual.
    assert rows[0]["transforms"]["flow::idiosyncratic"] == pytest.approx(0.12)
    assert rows[-1]["transforms"]["flow::idiosyncratic"] == pytest.approx(-0.48)
    assert summary["observations_decomposed"] == 5


def test_residualization_declines_when_the_cross_section_is_too_thin():
    moment = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    rows = [_observation("AAA", moment, -0.3), _observation("BBB", moment, -0.2)]
    residualize_cross_section(rows, feature="flow", sectors={}, minimum_peers=4)
    assert rows[0]["transforms"]["flow::idiosyncratic"] is None


def test_cross_sectional_dependence_flags_one_market_bet_wearing_many_tickers():
    moment = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    rows = [_observation(f"S{index}", moment, -0.3) for index in range(8)]
    for row in rows:
        row["transforms"] = {"flow::raw": row["features"]["flow"]}
    report = cross_sectional_dependence(rows, feature="flow")
    assert report["same_sign_share"] == pytest.approx(1.0)
    assert report["pseudo_diversification_warning"] is True


# ---------------------------------------------------------------------------
# Buckets, monotonicity, cost
# ---------------------------------------------------------------------------


def test_bucket_profile_is_equal_count_and_ordered_by_the_feature():
    pairs = [(float(index), float(index) / 10_000) for index in range(100)]
    profile = bucket_profile(pairs, buckets=10)
    assert len(profile) == 10
    assert all(row["observations"] == 10 for row in profile)
    assert profile[0]["mean_forward_bps"] < profile[-1]["mean_forward_bps"]


def test_monotonicity_separates_a_progression_from_a_single_spike():
    progression = [{"mean_forward_bps": value} for value in (0.1, 0.4, 0.9, 1.8, 3.7)]
    spike = [{"mean_forward_bps": value} for value in (0.1, -0.4, 0.2, 4.7, -3.1)]
    assert monotonicity(progression)["monotone_step_fraction"] == pytest.approx(1.0)
    assert monotonicity(progression)["rank_correlation"] == pytest.approx(1.0)
    assert monotonicity(spike)["monotone_step_fraction"] < 0.7


def test_cost_hurdle_requires_a_multiple_of_cost_not_break_even():
    hurdle = cost_hurdle(cost_bps=3.0, safety_multiple=2.0)
    assert hurdle["required_gross_bps"] == pytest.approx(6.0)
    assert hurdle["required_gross_bps_two_leg"] == pytest.approx(12.0)
    with pytest.raises(ValueError):
        cost_hurdle(cost_bps=3.0, safety_multiple=0.5)


def test_horizon_feasibility_kills_a_horizon_no_forecast_could_pay_for():
    rng = random.Random(3)
    observations = []
    for index in range(200):
        move = rng.gauss(0, 0.00002)  # ~0.2bps of dispersion
        observations.append(
            {
                "cost_bps": 4.0,
                "forward": {
                    60: {"available": True, "gross_return_bps": move * 10_000},
                    1_800: {"available": True, "gross_return_bps": move * 10_000 * 100},
                },
            }
        )
    report = horizon_cost_feasibility(observations, horizons_seconds=(60, 1_800))
    assert report["60s"]["feasible"] is False
    assert report["60s"]["reason"] == "alpha_ceiling_below_cost"
    assert report["1800s"]["feasible"] is True


# ---------------------------------------------------------------------------
# Session-clustered inference
# ---------------------------------------------------------------------------


def test_ic_is_computed_inside_a_session_before_sessions_are_averaged():
    rows = []
    for day in range(30):
        session = date(2026, 1, 5) + timedelta(days=day)
        for index in range(10):
            rows.append(
                {
                    "session_date": session,
                    "value": float(index),
                    "forward": float(index) / 10_000,
                }
            )
    report = session_clustered_ic(rows)
    assert report["sessions_scored"] == 30
    assert report["rank_ic"] == pytest.approx(1.0)
    assert report["positive_session_share"] == pytest.approx(1.0)


def test_a_session_with_too_few_symbols_is_skipped_rather_than_scored():
    rows = [
        {"session_date": date(2026, 1, 5), "value": 1.0, "forward": 0.0001},
        {"session_date": date(2026, 1, 5), "value": 2.0, "forward": 0.0002},
    ]
    assert session_clustered_ic(rows)["sessions_scored"] == 0


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def _cell_rows(edge_bps: float, *, sessions: int = 80, symbols: int = 20, seed: int = 11):
    rng = random.Random(seed)
    rows = []
    for day in range(sessions):
        session = date(2026, 1, 5) + timedelta(days=day)
        for index in range(symbols):
            value = rng.gauss(0, 1)
            forward = (edge_bps * value + rng.gauss(0, 6)) / 10_000
            rows.append(
                {
                    "value": value,
                    "forward": forward,
                    "cost_bps": 2.0,
                    "symbol": f"S{index:02d}",
                    "session_date": session,
                }
            )
    return rows


CELL = CellKey("flow", "raw", 60, "all", "all")


def test_a_thin_cell_reports_insufficient_data_rather_than_a_verdict():
    result = measure_cell(_cell_rows(20.0, sessions=5, symbols=4), cell=CELL)
    assert result["verdict"] == VERDICT_INSUFFICIENT


def test_pure_noise_is_no_information_not_a_small_edge():
    result = measure_cell(_cell_rows(0.0), cell=CELL, effective_trials=50)
    assert result["verdict"] == VERDICT_NO_INFORMATION


def test_a_real_but_tiny_forecast_is_information_below_cost_not_no_information():
    result = measure_cell(_cell_rows(2.0), cell=CELL, effective_trials=50)
    assert result["verdict"] == VERDICT_BELOW_COST
    # The distinction is the product: this says change the expression, while
    # no_information says retire the feature.
    assert "not harvestable" in " ".join(result["reasons"])
    assert result["rank_ic"] > 0


def test_a_large_stable_forecast_clears_the_hurdle():
    result = measure_cell(_cell_rows(15.0), cell=CELL, effective_trials=50)
    assert result["verdict"] == VERDICT_TRADABLE
    assert result["extreme_bucket_gross_bps"] > result["cost_hurdle"]["required_gross_bps"]
    assert result["monotonicity"]["monotone_step_fraction"] >= 0.7


def test_an_inverse_relationship_is_expressed_the_right_way_round():
    # A negative relationship is traded by buying the bottom bucket or selling
    # the top one. Hard-coding "long the top bucket" would report a perfectly
    # good inverse signal as a loss.
    result = measure_cell(_cell_rows(-15.0), cell=CELL, effective_trials=50)
    assert result["rank_ic"] < 0
    assert result["extreme_side"] in {"long_bottom_bucket", "short_top_bucket"}
    assert result["extreme_bucket_gross_bps"] > 0
    assert result["verdict"] == VERDICT_TRADABLE


def test_the_tested_extreme_is_chosen_by_the_ic_sign_not_by_which_looked_better():
    # Picking whichever extreme happened to be larger and then testing it is a
    # selection the significance test cannot see, and under noise it is
    # reliably positive. Positive and negative relationships of the same size
    # must therefore produce mirror-image expressions, not the same one.
    positive = measure_cell(_cell_rows(15.0), cell=CELL, effective_trials=50)
    negative = measure_cell(_cell_rows(-15.0), cell=CELL, effective_trials=50)
    assert positive["extreme_bucket_end"] != negative["extreme_bucket_end"] or (
        positive["extreme_trade_sign"] != negative["extreme_trade_sign"]
    )
    assert positive["extreme_bucket_gross_bps"] == pytest.approx(
        negative["extreme_bucket_gross_bps"], rel=0.35
    )


# ---------------------------------------------------------------------------
# Selection pressure
# ---------------------------------------------------------------------------


def test_pbo_is_high_when_the_ranking_of_cells_is_noise():
    rng = random.Random(5)
    sessions = [date(2026, 1, 5) + timedelta(days=index) for index in range(60)]
    performance = {
        f"cell_{index}": {session: rng.gauss(0, 1) for session in sessions}
        for index in range(12)
    }
    report = probability_of_backtest_overfitting(performance)
    assert report["probability"] > 0.3


def test_pbo_is_low_when_one_cell_is_genuinely_better_everywhere():
    sessions = [date(2026, 1, 5) + timedelta(days=index) for index in range(60)]
    rng = random.Random(5)
    performance = {
        f"cell_{index}": {session: rng.gauss(0, 0.2) for session in sessions}
        for index in range(1, 12)
    }
    performance["cell_0"] = {session: 5.0 + rng.gauss(0, 0.2) for session in sessions}
    report = probability_of_backtest_overfitting(performance)
    assert report["probability"] == pytest.approx(0.0)


def test_the_declared_grid_enumerates_slice_values_not_slice_kinds():
    # Slicing by symbol across 40 names is 40 more looks at the same data.
    # Charging it as one look would put the deflated Sharpe back to flattering
    # exactly the searches it exists to discount.
    keys = declared_cell_keys(
        features=["flow"],
        transforms=("raw", "idiosyncratic"),
        horizons_seconds=(60, 300),
        slices=("all", "symbol"),
        symbols=["AAPL", "NVDA"],
        signal_timeframe="30m",
    )
    assert len(keys) == 1 * 2 * 2 * (1 + 2)
    assert "flow::raw::300s::symbol=NVDA" in keys


def test_time_of_day_slices_follow_the_signal_timeframe_grid():
    def count(timeframe: str) -> int:
        return len(
            declared_cell_keys(
                features=["flow"],
                transforms=("raw",),
                horizons_seconds=(60,),
                slices=("time_of_day",),
                symbols=["AAPL"],
                signal_timeframe=timeframe,
            )
        )

    assert count("30m") == 13
    assert count("15m") == 26


def test_pbo_declines_to_answer_without_enough_sessions():
    performance = {"a": {date(2026, 1, 5): 1.0}, "b": {date(2026, 1, 5): 2.0}}
    assert probability_of_backtest_overfitting(performance)["probability"] is None


# ---------------------------------------------------------------------------
# The whole map
# ---------------------------------------------------------------------------


def _panel(edges: dict[int, float], *, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    symbols = [f"S{index:02d}" for index in range(20)]
    observations: list[dict] = []
    for day in range(80):
        session_start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC) + timedelta(days=day)
        for bar in range(12):
            decision = session_start + timedelta(minutes=30 * (bar + 1))
            market = rng.gauss(0, 0.0004)
            for symbol in symbols:
                value = rng.gauss(0, 1)
                observations.append(
                    {
                        "symbol": symbol,
                        "timestamp": decision,
                        "session_date": session_start.date(),
                        "slot": f"{bar:02d}:00",
                        "minutes_from_open": 30 * bar,
                        "features": {"flow": value},
                        "cost_bps": 2.0,
                        "forward": {
                            horizon: {
                                "available": True,
                                "gross_return": edge * value / 10_000
                                + rng.gauss(0, 0.0006)
                                + market,
                                "gross_return_bps": 0.0,
                            }
                            for horizon, edge in edges.items()
                        },
                    }
                )
    for observation in observations:
        for outcome in observation["forward"].values():
            outcome["gross_return_bps"] = outcome["gross_return"] * 10_000
    return observations


def test_the_map_finds_where_information_lives_and_where_it_has_decayed():
    result = measure_alpha_map(
        _panel({60: 12.0, 300: 2.0, 1_800: 0.0}),
        features=["flow"],
        horizons_seconds=(60, 300, 1_800),
        grid_seconds=60,
        transforms=("raw",),
    )
    by_horizon = {cell["horizon_seconds"]: cell["verdict"] for cell in result["cells"]}
    # This is exactly the shape a 30m-only research primitive cannot see: a
    # strong one-minute forecast that is gone well before the half hour.
    assert by_horizon[60] == VERDICT_TRADABLE
    assert by_horizon[300] == VERDICT_BELOW_COST
    assert by_horizon[1_800] == VERDICT_NO_INFORMATION
    assert result["strategy_construction_authorized"] is True
    assert "surviving cells" in result["kill_summary"]["flow"]["recommendation"]


def test_a_feature_with_nothing_in_it_authorizes_nothing():
    result = measure_alpha_map(
        _panel({60: 0.0, 300: 0.0, 1_800: 0.0}),
        features=["flow"],
        horizons_seconds=(60, 300, 1_800),
        grid_seconds=60,
        transforms=("raw",),
    )
    assert result["verdict_counts"] == {VERDICT_NO_INFORMATION: 3}
    assert result["strategy_construction_authorized"] is False
    assert result["survivors"] == []
    assert result["kill_summary"]["flow"]["recommendation"].startswith("retire")


def test_the_grid_is_charged_to_the_trial_count_by_default():
    result = measure_alpha_map(
        _panel({60: 0.0, 300: 0.0}),
        features=["flow"],
        horizons_seconds=(60, 300),
        grid_seconds=60,
        transforms=("raw", "zscore_symbol_tod"),
    )
    # Two transforms times two horizons is four looks at the same data, and the
    # trial count has to say four whether or not four numbers get reported.
    assert result["declared_cells"] == 4
    assert result["effective_trials"] == 4


def test_unavailable_horizons_are_reported_rather_than_silently_dropped():
    result = measure_alpha_map(
        _panel({60: 0.0}),
        features=["flow"],
        horizons_seconds=(10, 30, 60),
        grid_seconds=60,
        transforms=("raw",),
    )
    assert result["horizons"]["available_seconds"] == [60]
    assert set(result["horizons"]["unavailable"]) == {"10s", "30s"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_declares_with_the_documented_defaults():
    args = parser().parse_args(
        ["declare", "--dataset-id", "82", "--symbols", "aapl,nvda", "--cost-calibration-id", "3"]
    )
    assert args.symbols == ["AAPL", "NVDA"]
    assert args.grid_timeframe == "1m"
    assert args.signal_timeframe == "30m"
    assert args.cost_safety_multiple == DEFAULT_COST_SAFETY_MULTIPLE


def test_cli_rejects_an_unknown_transform():
    args = parser().parse_args(
        [
            "declare",
            "--dataset-id",
            "82",
            "--symbols",
            "AAPL",
            "--transforms",
            "raw,made_up",
        ]
    )
    assert sorted(set(args.transforms) - set(TRANSFORMS)) == ["made_up"]


# ---------------------------------------------------------------------------
# Real PostgreSQL integration
# ---------------------------------------------------------------------------


def test_run_alpha_map_persists_the_same_cells_as_the_python_measurement_on_postgres():
    """Validate migration 077 through the production persistence path.

    This is deliberately skipped by default because it requires a real migrated
    PostgreSQL database.  Enable it only against a disposable database:

        KEFTRADE_RUN_POSTGRES_INTEGRATION=1 DATABASE_URL=postgresql://...

    The test seeds a deterministic 30m signal / 1m outcome fixture, declares an
    Alpha Map, runs the same measurement two ways, and verifies that the rows in
    ``intraday_alpha_map_cells`` match the pure Python result.  That is the
    contract we care about: real PostgreSQL -> run_alpha_map ->
    intraday_alpha_map_cells equals the expected calculation.
    """

    if os.getenv("KEFTRADE_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set KEFTRADE_RUN_POSTGRES_INTEGRATION=1 to run the PostgreSQL test")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the PostgreSQL integration test")

    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    token = uuid4().hex[:10].upper()
    symbols = [f"AM{token[:2]}{index:02d}" for index in range(8)]
    features = ["signed_trade_imbalance"]
    transforms = (
        "raw",
        "zscore_symbol_tod",
        "percentile_symbol_tod",
        "idiosyncratic",
    )
    horizons = (60, 120, 300, 600, 900, 1_800, 3_600)

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        _seed_alpha_map_postgres_fixture(conn, token=token, symbols=symbols)

        dataset_id = conn.execute(
            "SELECT id FROM research_dataset_manifests WHERE dataset_key = %s",
            (f"alpha-map-postgres-fixture-{token}",),
        ).fetchone()["id"]
        cost_id = conn.execute(
            """
            SELECT id
            FROM intraday_execution_cost_calibrations
            WHERE calculation_version = %s
            """,
            (f"alpha_map_postgres_fixture_{token}",),
        ).fetchone()["id"]

        declaration = declare_alpha_map(
            conn,
            dataset_id=int(dataset_id),
            signal_timeframe="30m",
            grid_timeframe="1m",
            symbols=symbols,
            features=features,
            horizons_seconds=horizons,
            transforms=transforms,
            slices=("all",),
            cost_calibration_id=int(cost_id),
            cost_safety_multiple=2.0,
            feed="sip",
            source="alpaca",
        )
        declaration_id = int(declaration["id"])

        splits = get_dataset_splits(conn, int(dataset_id))
        assert splits is not None
        cost_model = load_cost_model(conn, int(cost_id))
        panel = load_alpha_map_panel(
            conn,
            symbols=symbols,
            signal_timeframe="30m",
            grid_timeframe="1m",
            start=splits.discovery_start,
            end=splits.validation_start,
            feed="sip",
            source="alpaca",
            cost_model=cost_model,
        )
        observations = panel["observations"]
        attach_forward_returns(
            observations,
            horizons_seconds=horizons,
            grid_seconds=panel["grid_seconds"],
        )
        expected = measure_alpha_map(
            observations,
            features=features,
            horizons_seconds=horizons,
            grid_seconds=panel["grid_seconds"],
            sectors=sector_map(conn, symbols),
            transforms=transforms,
            slices=("all",),
            safety_multiple=2.0,
            effective_trials=int(declaration["declared_cell_count"]),
        )

        actual = run_alpha_map(conn, declaration_id=declaration_id, phase="discovery")
        run_id = int(actual["run_id"])
        persisted = conn.execute(
            """
            SELECT cell_key, feature, feature_transform, horizon_seconds,
                   slice_kind, slice_value, observations, distinct_sessions,
                   distinct_symbols, rank_ic, rank_ic_t_statistic,
                   extreme_bucket_gross_bps, long_short_gross_bps,
                   estimated_round_trip_cost_bps, required_gross_bps,
                   net_bps, monotonicity, verdict
            FROM intraday_alpha_map_cells
            WHERE run_id = %s
            ORDER BY cell_key
            """,
            (run_id,),
        ).fetchall()

        expected_by_key = {cell["cell_key"]: cell for cell in expected["cells"]}
        assert [row["cell_key"] for row in persisted] == sorted(expected_by_key)
        assert len(persisted) == 28

        for row in persisted:
            cell = expected_by_key[row["cell_key"]]
            assert row["feature"] == cell["feature"]
            assert row["feature_transform"] == cell["feature_transform"]
            assert row["horizon_seconds"] == cell["horizon_seconds"]
            assert row["slice_kind"] == cell["slice_kind"]
            assert row["slice_value"] == cell["slice_value"]
            assert row["observations"] == cell["observations"]
            assert row["distinct_sessions"] == cell["distinct_sessions"]
            assert row["distinct_symbols"] == cell["distinct_symbols"]
            assert row["verdict"] == cell["verdict"]
            assert _maybe_float(row["rank_ic"]) == pytest.approx(cell.get("rank_ic"))
            assert _maybe_float(row["rank_ic_t_statistic"]) == pytest.approx(
                cell.get("rank_ic_t_statistic")
            )
            assert _maybe_float(row["extreme_bucket_gross_bps"]) == pytest.approx(
                cell.get("extreme_bucket_gross_bps")
            )
            assert _maybe_float(row["long_short_gross_bps"]) == pytest.approx(
                cell.get("long_short_gross_bps")
            )
            assert _maybe_float(row["estimated_round_trip_cost_bps"]) == pytest.approx(
                (cell.get("cost_hurdle") or {}).get("estimated_round_trip_cost_bps")
            )
            assert _maybe_float(row["required_gross_bps"]) == pytest.approx(
                (cell.get("cost_hurdle") or {}).get("required_gross_bps")
            )
            assert _maybe_float(row["net_bps"]) == pytest.approx(cell.get("net_bps"))
            assert _maybe_float(row["monotonicity"]) == pytest.approx(
                (cell.get("monotonicity") or {}).get("rank_correlation")
            )

        run_row = conn.execute(
            """
            SELECT observation_count, effective_trials,
                   strategy_construction_authorized, survivors
            FROM intraday_alpha_map_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
        assert run_row["observation_count"] == expected["observations"]
        assert run_row["effective_trials"] == expected["effective_trials"]
        assert run_row["strategy_construction_authorized"] == expected[
            "strategy_construction_authorized"
        ]
        assert list(run_row["survivors"] or []) == expected["survivors"]

        with pytest.raises(ValueError, match="already measured"):
            run_alpha_map(conn, declaration_id=declaration_id, phase="discovery")


def _maybe_float(value):
    return None if value is None else float(value)


def _seed_alpha_map_postgres_fixture(conn, *, token: str, symbols: list[str]) -> None:
    from psycopg.types.json import Jsonb

    start = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    sessions = 60
    signal_minutes = 30
    grid_minutes = 1
    dataset_key = f"alpha-map-postgres-fixture-{token}"

    for index, symbol in enumerate(symbols):
        conn.execute(
            """
            INSERT INTO symbols(symbol, asset_class, exchange, currency, name,
                                provider_symbol, primary_provider, sector)
            VALUES (%s, 'equity', 'TEST', 'USD', %s, %s, 'fixture', %s)
            ON CONFLICT (symbol) DO NOTHING
            """,
            (symbol, f"Alpha Map Fixture {symbol}", symbol, f"sector_{index % 2}"),
        )

    dataset_id = conn.execute(
        """
        INSERT INTO research_dataset_manifests(
            dataset_key, name, mode, assets, timeframes, window_start, window_end,
            candle_counts, candle_hashes, source_providers, content_hash,
            integrity, calculation_version
        )
        VALUES (%s,%s,'reproducibility',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            dataset_key,
            "Alpha Map PostgreSQL fixture",
            Jsonb(symbols),
            Jsonb(["1m", "30m"]),
            start,
            start + timedelta(days=sessions + 1, minutes=90),
            Jsonb({"1m": sessions * len(symbols) * 70, "30m": sessions * len(symbols)}),
            Jsonb({}),
            Jsonb(["alpaca"]),
            f"alpha-map-postgres-fixture-content-{token}",
            Jsonb({"purpose": "deterministic integration test"}),
            f"alpha_map_postgres_fixture_{token}",
        ),
    ).fetchone()["id"]

    conn.execute(
        """
        INSERT INTO research_dataset_splits(
            dataset_id, discovery_start, discovery_end, validation_start,
            validation_end, confirmation_start, confirmation_end, split_version
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            dataset_id,
            start - timedelta(minutes=1),
            start + timedelta(days=sessions - 1, hours=1),
            start + timedelta(days=sessions, hours=2),
            start + timedelta(days=sessions + 1, hours=2),
            start + timedelta(days=sessions + 2, hours=2),
            start + timedelta(days=sessions + 3, hours=2),
            f"alpha_map_postgres_fixture_{token}",
        ),
    )

    conn.execute(
        """
        INSERT INTO intraday_execution_cost_calibrations(
            provider, feed, window_start, window_end, symbols, quote_observations,
            matched_fill_observations, regulatory_bps, median_spread_bps,
            p90_spread_bps, observed_round_trip_bps, stressed_round_trip_bps,
            conservative_round_trip_bps, by_symbol, by_time_slot, methodology,
            calculation_version
        )
        VALUES ('fixture','sip',%s,%s,%s,1000,0,0,0.10,0.20,0.20,0.20,0.20,
                %s,%s,%s,%s)
        """,
        (
            start,
            start + timedelta(days=sessions + 1),
            Jsonb(symbols),
            Jsonb({}),
            Jsonb({}),
            Jsonb({"kind": "deterministic fixture", "round_trip_bps": 0.20}),
            f"alpha_map_postgres_fixture_{token}",
        ),
    )

    values = [-0.90, -0.65, -0.35, -0.10, 0.10, 0.35, 0.65, 0.90]
    for day in range(sessions):
        signal_open = start + timedelta(days=day)
        decision = signal_open + timedelta(minutes=signal_minutes)
        session = signal_open.astimezone().date()
        for symbol_index, symbol in enumerate(symbols):
            raw = values[(symbol_index + day) % len(values)]
            base_price = 100.0 + symbol_index * 3.0 + day * 0.01

            conn.execute(
                """
                INSERT INTO candles(symbol, source, timeframe, timestamp, open, high, low, close, volume)
                VALUES (%s,'alpaca','30m',%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, source, timeframe, timestamp) DO NOTHING
                """,
                (
                    symbol,
                    signal_open,
                    base_price,
                    base_price * 1.003,
                    base_price * 0.997,
                    base_price * (1.0 + raw * 0.0001),
                    100_000 + symbol_index * 1_000,
                ),
            )
            conn.execute(
                """
                INSERT INTO intraday_features(
                    symbol, timeframe, timestamp, session_date, minutes_from_open,
                    minutes_to_close, session_vwap, distance_from_session_vwap,
                    opening_range_high, opening_range_low, opening_range_position,
                    gap_percent, session_relative_volume, opening_range_minutes,
                    relative_volume_lookback_sessions
                )
                VALUES (%s,'30m',%s,%s,30,330,%s,%s,%s,%s,0.5,0.0,1.0,30,20)
                ON CONFLICT (symbol, timeframe, timestamp) DO NOTHING
                """,
                (
                    symbol,
                    signal_open,
                    session,
                    base_price,
                    raw * 0.001,
                    base_price * 1.004,
                    base_price * 0.996,
                ),
            )
            conn.execute(
                """
                INSERT INTO intraday_trade_flow_features(
                    symbol, timeframe, timestamp, provider, feed, trade_count,
                    total_volume, buy_volume, sell_volume, signed_trade_imbalance,
                    signed_trade_count_imbalance, large_trade_share,
                    unclassified_share, trade_vwap, effective_spread_bps,
                    calculation_version
                )
                VALUES (%s,'30m',%s,'alpaca','sip',1000,%s,%s,%s,%s,%s,0.10,0.0,%s,0.20,
                        'intraday_trade_flow_v2_fixture')
                ON CONFLICT (symbol, timeframe, timestamp, provider, feed) DO NOTHING
                """,
                (
                    symbol,
                    signal_open,
                    100_000,
                    50_000 * (1.0 + raw),
                    50_000 * (1.0 - raw),
                    raw,
                    raw,
                    base_price,
                ),
            )

            for minute in range(70):
                ts = decision + timedelta(minutes=minute)
                # Strong deterministic relationship so the DB-backed run has
                # non-null cells across every requested horizon.  The exact
                # verdict is less important than exact equality with the Python
                # measurement below.
                open_price = base_price
                close_price = base_price * (1.0 + raw * 0.000025 * (minute + 1))
                high = max(open_price, close_price) * 1.0001
                low = min(open_price, close_price) * 0.9999
                conn.execute(
                    """
                    INSERT INTO candles(symbol, source, timeframe, timestamp,
                                        open, high, low, close, volume)
                    VALUES (%s,'alpaca','1m',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, source, timeframe, timestamp) DO NOTHING
                    """,
                    (
                        symbol,
                        ts,
                        open_price,
                        high,
                        low,
                        close_price,
                        3_000 + minute * 10 + symbol_index,
                    ),
                )

    conn.commit()
