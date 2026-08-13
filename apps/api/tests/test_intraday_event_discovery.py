from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.cli.intraday_event_discovery import parser
from app.services.intraday_event_discovery import (
    BRANCH_FAILED_AUCTION,
    BRANCH_GAP,
    BRANCH_ONE_MINUTE_VETO,
    FEATURE_CATALOG,
    _apply_frozen_models,
    _build_models,
    _confirmation_gates,
    _detect_failed_auctions,
    _normalization_model,
    _outcomes,
    _validate_branches,
)


def _bar(index: int, *, open_: float, high: float, low: float, close: float):
    return {
        "timestamp": datetime(2026, 1, 5, 14, 30, tzinfo=UTC) + timedelta(minutes=15 * index),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    }


def _event(index: int, phase: str, outcome_bps: float, alpha: float, spread: float):
    timestamp = datetime(2025, 1, 2, 15, 0, tzinfo=UTC) + timedelta(days=index)
    return {
        "event_key": "15m_failed_auction_confirmed",
        "branch": BRANCH_FAILED_AUCTION,
        "stage": "range_reentry",
        "symbol": f"S{index % 8}",
        "session_date": timestamp.date(),
        "decision_timestamp": timestamp,
        "direction": "short",
        "phase": phase,
        "cost_bps": 2.0,
        "labels": {},
        "features": {
            "excursion_atr": alpha,
            "progress_efficiency": 1 - alpha,
            "reentry_depth_atr": alpha,
            "flow_exhaustion": alpha,
            "minutes_from_open": 60,
            "relative_volume_surprise": 1 + alpha,
            "effort_result_ratio": 2 + alpha,
            "directional_flow_shift": alpha,
            "large_trade_share": alpha,
            "idiosyncratic_return": alpha / 100,
            "adverse_market_alignment": False,
            "effective_spread_bps": spread,
            "market_return": 0.0,
        },
        "outcomes": {
            "30m": {
                "available": True,
                "net_return": outcome_bps / 10_000,
                "net_return_bps": outcome_bps,
                "mae_bps": 10 if outcome_bps > 0 else 50,
            }
        },
    }


def test_fixed_horizon_outcomes_charge_cost_and_measure_mfe_mae():
    session = [
        _bar(0, open_=100, high=101, low=99, close=100),
        _bar(1, open_=100, high=103, low=99.5, close=102),
        _bar(2, open_=102, high=104, low=101, close=103),
    ]

    result = _outcomes(
        session,
        decision_index=0,
        direction="long",
        timeframe="15m",
        cost_bps=5.0,
        horizons=(15, 30, 60),
    )

    assert result["15m"]["gross_return_bps"] == pytest.approx(200.0)
    assert result["15m"]["net_return_bps"] == pytest.approx(195.0)
    assert result["15m"]["mfe_bps"] == pytest.approx(300.0)
    assert result["15m"]["mae_bps"] == pytest.approx(50.0)
    assert result["30m"]["exit_price"] == 103
    assert result["60m"]["available"] is False


def test_failed_auction_detector_keeps_probe_and_separate_range_reentry_event():
    ny = ZoneInfo("America/New_York")
    rows = []
    for index in range(12):
        timestamp = (
            datetime(2026, 1, 5, 9, 30, tzinfo=ny) + timedelta(minutes=15 * index)
        ).astimezone(UTC)
        open_price = 100.0
        high = 100.1
        low = 99.9
        close = 100.0
        if index == 4:
            high, low, close = 102.0, 99.9, 101.2
        if index == 5:
            high, low, close = 101.3, 99.6, 99.8
        rows.append(
            {
                "symbol": "TEST",
                "timeframe": "15m",
                "timestamp": timestamp,
                "session_date": date(2026, 1, 5),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000,
                "session_relative_volume": 1.6,
                "minutes_from_open": 15 * index,
                "flow_signed_trade_imbalance": 0.4 if index == 4 else 0.0,
                "flow_large_trade_share": 0.2,
                "flow_effective_spread_bps": 2.0,
            }
        )
    contexts = {
        "returns": {},
        "sector": {},
        "market": {},
        "overnight": {},
        "overnight_sector": {},
        "overnight_market": {},
    }

    events = _detect_failed_auctions(
        {"TEST": rows},
        timeframe="15m",
        contexts=contexts,
        sectors={},
        cost_model={"stressed_round_trip_bps": 2.0, "conservative_round_trip_bps": 30.0},
        horizons=(15, 30, 60),
    )

    assert {event["stage"] for event in events} >= {"breakout_probe", "range_reentry"}
    probe = next(event for event in events if event["stage"] == "breakout_probe")
    confirmed = next(event for event in events if event["stage"] == "range_reentry")
    assert probe["labels"]["failure_within_2_bars"] is True
    assert confirmed["decision_timestamp"] > probe["decision_timestamp"]
    assert confirmed["outcomes"]["30m"]["available"] is True

    before_session = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    withheld = _detect_failed_auctions(
        {"TEST": rows},
        timeframe="15m",
        contexts=contexts,
        sectors={},
        cost_model={"stressed_round_trip_bps": 2.0, "conservative_round_trip_bps": 30.0},
        horizons=(15, 30, 60),
        decision_end=before_session,
    )
    assert withheld == []


def test_normalization_is_fit_only_on_the_rows_passed_to_it():
    development = [_event(index, "discovery", 5, index / 10, 2) for index in range(10)]
    held_out = _event(20, "validation", 5, 99, 2)

    model = _normalization_model(development, ["excursion_atr"])

    assert max(model["excursion_atr"]["global"]) < held_out["features"]["excursion_atr"]


def test_model_freezes_score_and_uses_same_threshold_on_later_rows():
    discovery = [
        _event(index, "discovery", -5 + index, index / 40, 2 + index / 100)
        for index in range(40)
    ]
    validation = [
        _event(100 + index, "validation", -3 + index, index / 20, 2 + index / 100)
        for index in range(20)
    ]
    models, reports, trials = _build_models(
        discovery + validation,
        branches=[BRANCH_FAILED_AUCTION],
    )

    assert trials > 1
    assert "15m_failed_auction_confirmed" in reports
    assert models["15m_failed_auction_confirmed"]["selection_threshold"] is not None

    confirmation = [_event(200 + index, "confirmation", 10, index / 10, 2) for index in range(10)]
    _apply_frozen_models(confirmation, models)

    assert any(row["features"]["_selected"] for row in confirmation)


def test_confirmation_requires_real_independent_post_cost_evidence():
    passing = {
        "signals": 80,
        "mean_net_bps": 4.0,
        "day_clustered_t_statistic": 3.5,
        "block_bootstrap": {"confidence_interval_95": [1.0, 8.0]},
        "deflated_sharpe": {"deflated_sharpe": 0.97},
        "independent_evidence_ready": True,
    }
    failing = {**passing, "mean_net_bps": -0.1}

    assert all(_confirmation_gates(passing).values())
    assert _confirmation_gates(failing)["positive_net_expectancy"] is False


def test_branch_timeframes_keep_1m_as_veto_only():
    assert _validate_branches("30m", [BRANCH_GAP, BRANCH_ONE_MINUTE_VETO]) == [
        BRANCH_GAP,
        BRANCH_ONE_MINUTE_VETO,
    ]
    assert _validate_branches("15m", [BRANCH_FAILED_AUCTION]) == [BRANCH_FAILED_AUCTION]
    assert all(spec.role == "veto" for spec in FEATURE_CATALOG[BRANCH_ONE_MINUTE_VETO])


def test_one_minute_model_passes_parent_event_through_and_only_learns_vetoes():
    rows = []
    for index in range(60):
        row = _event(index, "discovery" if index < 40 else "validation", 5, 0.5, 2)
        row["event_key"] = "30m_gap_absorption_1m_veto"
        row["branch"] = BRANCH_ONE_MINUTE_VETO
        row["stage"] = "pre_entry_veto"
        row["outcomes"] = {
            "60m": {
                "available": True,
                "net_return": 0.0005,
                "net_return_bps": 5.0,
                "mae_bps": 10.0,
            }
        }
        row["features"] = {
            "intrabar_directional_return": 0.001,
            "intrabar_last_5m_directional_return": 0.001,
            "intrabar_directional_imbalance": 0.1,
            "intrabar_imbalance_improvement": 0.1,
            "intrabar_spread_bps": 2.0,
        }
        rows.append(row)

    models, _, _ = _build_models(rows, branches=[BRANCH_ONE_MINUTE_VETO])
    model = models["30m_gap_absorption_1m_veto"]

    assert model["base_event_pass_through"] is True
    assert model["alpha_features"] == []
    assert model["selection_threshold"] == 1.0


def test_cli_exposes_governed_phases_without_a_trade_command():
    root = parser()
    choices = root._subparsers._group_actions[0].choices

    assert set(choices) == {"catalog", "declare", "discover", "report", "confirm"}
    assert "trade" not in choices
