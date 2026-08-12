from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.intraday_hypotheses import trade_imbalance_v2_hypotheses
from app.services.intraday_trade_imbalance_calibration import (
    CalibrationSpec,
    calibrate_predictor_distribution,
    eligible_rows,
)


def source_rows(*, sessions=12, symbols=4, bars=6):
    rows = []
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    for session_index in range(sessions):
        day = date(2026, 1, 5) + timedelta(days=session_index)
        for symbol_index in range(symbols):
            for bar_index in range(bars):
                rows.append(
                    {
                        "symbol": f"S{symbol_index:02}",
                        "timestamp": start
                        + timedelta(days=session_index, minutes=30 * bar_index),
                        "session_date": day,
                        "trade_count": 10_000,
                        "total_volume": 100_000 + symbol_index * 10_000,
                        "classified_volume": 100_000,
                        "trade_size_squared_sum": 1_000_000,
                        "effective_trade_count": 10_000,
                        "signed_trade_imbalance": (-1 if symbol_index % 2 else 1)
                        * (0.02 + bar_index * 0.03),
                        "unclassified_share": 0.01,
                        "classification_method": "tick_rule",
                        # A poisoned field proves calibration never copies or
                        # evaluates an outcome supplied by a caller.
                        "forward_return": object(),
                    }
                )
    return rows


def small_spec():
    return CalibrationSpec(
        minimum_symbol_sessions=48,
        minimum_sessions=12,
        minimum_symbols=4,
        minimum_eligible_bars=288,
        bootstrap_samples=40,
        null_draws_per_bar=16,
        minimum_bucket_bars=8,
        maximum_relative_ci_half_width=0.5,
    )


def test_calibration_is_return_blind_and_deterministic():
    first, frozen = calibrate_predictor_distribution(source_rows(), spec=small_spec())
    second, _ = calibrate_predictor_distribution(source_rows(), spec=small_spec())

    assert first == second
    assert first["return_blind"] is True
    assert first["outcome_fields_accessed"] == []
    assert first["ready_for_declaration"] is True
    assert all("forward_return" not in row for row in frozen)
    assert first["threshold"]["global_rounded_up"] >= first["threshold"]["global_raw"]


def test_calibration_refuses_to_declare_below_the_predeclared_minimums():
    report, _ = calibrate_predictor_distribution(source_rows(sessions=2), spec=small_spec())

    assert report["ready_for_declaration"] is False
    assert "minimum_1500_symbol_sessions" in report["refusal_reasons"]


def test_quality_gates_require_second_moment_for_the_random_sign_null():
    rows = source_rows()
    rows[0]["trade_size_squared_sum"] = None

    eligible, excluded = eligible_rows(rows, spec=small_spec())

    assert len(eligible) == len(rows) - 1
    assert excluded["missing_or_weak_null_moments"] == 1


def test_v2_hypotheses_freeze_the_calibration_identity_and_keep_both_horizons():
    report, _ = calibrate_predictor_distribution(source_rows(), spec=small_spec())
    calibration = {
        "id": 7,
        "ready_for_declaration": True,
        "dataset_hash": "dataset-hash",
        "specification_hash": report["specification_hash"],
        "calculation_version": report["calculation_version"],
        "report": report,
    }

    hypotheses = trade_imbalance_v2_hypotheses(calibration=calibration)

    assert {item.factor_key for item in hypotheses} == {
        "signed_trade_imbalance_continuation_v2_1bar",
        "signed_trade_imbalance_continuation_v2_2bar",
        "signed_trade_imbalance_exhaustion_reversal_v3_1bar",
        "signed_trade_imbalance_exhaustion_reversal_v3_2bar",
    }
    assert {item.horizon_bars for item in hypotheses} == {1, 2}
    assert {item.version for item in hypotheses} == {2, 3}
    assert all(dict(item.parameters)["calibration_id"] == 7 for item in hypotheses)
    assert {
        item.factor_key
        for item in hypotheses
        if dict(item.parameters).get("signal_polarity") == "reversal"
    } == {
        "signed_trade_imbalance_exhaustion_reversal_v3_1bar",
        "signed_trade_imbalance_exhaustion_reversal_v3_2bar",
    }


def test_v2_hypothesis_refuses_an_uncertified_calibration():
    with pytest.raises(ValueError, match="passed all gates"):
        trade_imbalance_v2_hypotheses(
            calibration={"id": 3, "ready_for_declaration": False}
        )
