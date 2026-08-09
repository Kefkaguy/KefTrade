from typing import Any

from app.services.intraday_factor_diagnostics import (
    DEFAULT_FACTOR_KEYS,
    FACTOR_SPECS,
    FactorSpec,
    first_to_last_half_hour_observations,
)
from app.services.intraday_research_controls import synthetic_intraday_candles
from app.services.intraday_research_leakage import (
    audit_factor_leakage,
    future_perturbation_report,
    perturb_future_candles,
    timing_assertions,
)
from app.services.intraday_session_calendar import (
    bar_close_timestamp,
    closing_bar,
    opening_bar,
    ordered_regular_sessions,
)

AUDITABLE_KEYS = [
    key
    for key in DEFAULT_FACTOR_KEYS
    if not FACTOR_SPECS[key].requires_quotes
    and not FACTOR_SPECS[key].requires_auction_data
]


def market(sessions: int = 200, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
    return synthetic_intraday_candles(
        symbols=("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD"),
        sessions=sessions,
        injected_effect_bps=8.0,
        gap_effect_bps=25.0,
        gap_probability=0.25,
        seed=3,
        **kwargs,
    )


def leaky_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **_: Any,
) -> list[dict[str, Any]]:
    """A builder that peeks: the score is the target it claims to predict."""
    output: list[dict[str, Any]] = []
    for symbol in ("SPY", "QQQ"):
        for session_date, session in ordered_regular_sessions(
            candles_by_symbol.get(symbol, []), timeframe=timeframe
        ):
            first_bar = opening_bar(session, timeframe=timeframe)
            last_bar = closing_bar(session, timeframe=timeframe)
            if first_bar is None or last_bar is None:
                continue
            last_open = float(last_bar["open"])
            if last_open <= 0:
                continue
            target = (float(last_bar["close"]) - last_open) / last_open
            output.append(
                {
                    "factor_key": "leaky",
                    "symbol": symbol,
                    "session_date": session_date,
                    "timestamp": last_bar["timestamp"],
                    # Reads the very bar it is supposed to forecast.
                    "score": target,
                    "target_return": target,
                    "signal_bar_timestamp": first_bar["timestamp"],
                    "decision_timestamp": bar_close_timestamp(
                        first_bar["timestamp"], timeframe=timeframe
                    ),
                    "entry_bar_timestamp": last_bar["timestamp"],
                    "exit_bar_timestamp": last_bar["timestamp"],
                    "horizon_bars": 1,
                }
            )
    return output


def stale_target_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """A builder whose target never reads the bars it claims to span."""
    return [
        {**row, "target_return": 0.001}
        for row in first_to_last_half_hour_observations(
            candles_by_symbol, timeframe=timeframe, **kwargs
        )
    ]


def test_perturbation_only_touches_bars_at_or_after_the_cut():
    candles = market(sessions=40)
    timestamps = sorted({row["timestamp"] for row in candles["SPY"]})
    cut = timestamps[len(timestamps) // 2]

    perturbed = perturb_future_candles(candles, cut=cut)

    before = [row for row in perturbed["SPY"] if row["timestamp"] < cut]
    after = [row for row in perturbed["SPY"] if row["timestamp"] >= cut]
    original = {row["timestamp"]: row for row in candles["SPY"]}
    assert all(row["close"] == original[row["timestamp"]]["close"] for row in before)
    assert all(row["close"] != original[row["timestamp"]]["close"] for row in after)


def test_every_shipped_factor_survives_the_leakage_audit():
    audit = audit_factor_leakage(
        FACTOR_SPECS,
        market(),
        timeframe="30m",
        factor_keys=AUDITABLE_KEYS,
    )

    assert audit["factors_failing"] == []
    assert audit["passed"] is True
    for key in AUDITABLE_KEYS:
        assert audit["factors"][key]["status"] == "audited", key


def test_audit_can_use_explicit_side_channel_cut_points():
    candles = market(sessions=80)
    explicit_cut = sorted({row["timestamp"] for row in candles["SPY"]})[120]

    audit = audit_factor_leakage(
        FACTOR_SPECS,
        candles,
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum"],
        cut_points=[explicit_cut],
    )

    assert audit["cut_points"] == [str(explicit_cut)]
    assert audit["passed"] is True


def test_the_audit_catches_a_builder_that_reads_its_own_future():
    report = future_perturbation_report(
        leaky_observations,
        market(sessions=60),
        timeframe="30m",
        cut=sorted({row["timestamp"] for row in market(sessions=60)["SPY"]})[30],
    )

    assert report["scores_changed_by_future_data"] > 0
    assert report["checks"]["no_score_reads_the_future"] is False
    assert report["passed"] is False
    assert report["leak_examples"]


def test_the_audit_catches_a_target_that_ignores_the_bars_it_claims_to_span():
    candles = market(sessions=60)
    cut = sorted({row["timestamp"] for row in candles["SPY"]})[30]

    report = future_perturbation_report(
        stale_target_observations, candles, timeframe="30m", cut=cut
    )

    assert report["scores_changed_by_future_data"] == 0
    assert report["checks"]["targets_respond_to_future_bars"] is False
    assert report["passed"] is False


def test_a_factor_that_never_produced_an_observation_is_not_recorded_as_safe():
    audit = audit_factor_leakage(
        FACTOR_SPECS,
        # No gaps at all, so the gap family cannot be exercised.
        synthetic_intraday_candles(
            symbols=("SPY", "QQQ"), sessions=60, gap_probability=0.0, seed=5
        ),
        timeframe="30m",
        factor_keys=["gap_up_acceptance_continuation"],
    )

    result = audit["factors"]["gap_up_acceptance_continuation"]
    assert result["status"] == "not_exercised"
    assert result["passed"] is False
    assert audit["passed"] is False


def test_timing_assertions_reject_a_decision_taken_after_entry():
    candles = market(sessions=40)
    observations = first_to_last_half_hour_observations(candles, timeframe="30m")
    # Deciding at the entry bar's open is the tightest legitimate case, so the
    # violation has to be strictly later than that.
    broken = [
        {
            **row,
            "decision_timestamp": bar_close_timestamp(
                row["exit_bar_timestamp"], timeframe="30m"
            ),
        }
        for row in observations
    ]

    assert timing_assertions(observations, timeframe="30m")["passed"] is True
    report = timing_assertions(broken, timeframe="30m")
    assert report["checks"]["decision_precedes_entry"] is False
    assert report["observations_deciding_after_entry"] == len(broken)


def test_timing_assertions_reject_extended_hours_bars():
    candles = market(sessions=40)
    observations = first_to_last_half_hour_observations(candles, timeframe="30m")
    shifted = [
        {
            **row,
            "entry_bar_timestamp": row["exit_bar_timestamp"].replace(hour=23),
            "exit_bar_timestamp": row["exit_bar_timestamp"].replace(hour=23),
        }
        for row in observations
    ]

    report = timing_assertions(shifted, timeframe="30m")

    assert report["checks"]["regular_session_bars_only"] is False


def test_factor_spec_rejects_an_unknown_factor_type():
    try:
        FactorSpec(
            key="bad",
            title="bad",
            hypothesis="bad",
            supported_timeframes=("30m",),
            builder=leaky_observations,
            references=(),
            factor_type="whatever",
        )
    except ValueError as error:
        assert "factor_type" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an unknown factor_type must be refused")
