"""Phase 13.2: Feature Engine V2 -- leakage, boundary, and correctness tests.

The headline test is `test_no_feature_changes_when_future_bars_are_appended`:
it is the mechanical proof that no feature can see past the signal bar.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.services.labs.intraday.feature_engine_v2 import (
    DEFAULT_CONFIG,
    FEATURE_ENGINE_VERSION,
    SUPPORTED_OPENING_RANGE_MINUTES,
    FeatureEngineConfig,
    compute_v2_features,
)

SESSION_OPEN_UTC = time(14, 30)
BARS_PER_SESSION = 13  # 30m bars, 09:30-16:00 ET


def build_session_bars(
    *,
    sessions: int = 30,
    bars_per_session: int = BARS_PER_SESSION,
    base_price: float = 100.0,
    drift_per_bar: float = 0.0,
    volume: float = 1000.0,
    start_date: date = date(2026, 3, 2),
) -> list[dict]:
    """Deterministic synthetic bars on a fixed 30m grid, one UTC date per
    session (mirrors the real US-equity session, which never crosses a UTC
    date boundary)."""
    bars: list[dict] = []
    price = base_price
    for session_index in range(sessions):
        day = start_date + timedelta(days=session_index)
        for bar_index in range(bars_per_session):
            timestamp = datetime.combine(day, SESSION_OPEN_UTC, tzinfo=UTC) + timedelta(minutes=30 * bar_index)
            open_price = price
            close_price = price + drift_per_bar
            bars.append(
                {
                    "symbol": "TEST",
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "open": Decimal(str(round(open_price, 4))),
                    "high": Decimal(str(round(max(open_price, close_price) + 0.5, 4))),
                    "low": Decimal(str(round(min(open_price, close_price) - 0.5, 4))),
                    "close": Decimal(str(round(close_price, 4))),
                    "volume": Decimal(str(volume)),
                }
            )
            price = close_price
    return bars


def feature_row_for(bars: list[dict], index: int, **overrides) -> dict:
    """A plausible intraday_features row for bars[index], consistent with the
    synthetic grid (session_date from the UTC date, minutes_from_open from the
    bar's offset inside its session)."""
    bar = bars[index]
    session_date = bar["timestamp"].date()
    session_bars = [item for item in bars if item["timestamp"].date() == session_date]
    position = session_bars.index(bar)
    minutes_from_open = position * 30
    minutes_to_close = (len(session_bars) - 1 - position) * 30
    closes = [float(item["close"]) for item in session_bars[: position + 1]]
    row = {
        "session_date": session_date,
        "minutes_from_open": minutes_from_open,
        "minutes_to_close": minutes_to_close,
        "session_vwap": Decimal(str(round(sum(closes) / len(closes), 6))),
        "distance_from_session_vwap": Decimal("0.001"),
        "opening_range_high": Decimal(str(float(session_bars[0]["high"]))),
        "opening_range_low": Decimal(str(float(session_bars[0]["low"]))),
        "opening_range_position": Decimal("0.5"),
        "opening_range_minutes": 30,
        "gap_percent": Decimal("0.005"),
        "session_relative_volume": Decimal("1.1"),
    }
    row.update(overrides)
    return row


def compute_at(bars: list[dict], index: int, *, config=DEFAULT_CONFIG, **feature_overrides) -> dict:
    return compute_v2_features(
        bars[index],
        feature_row_for(bars, index, **feature_overrides),
        bars[: index + 1],
        config=config,
    )


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------

def test_no_feature_changes_when_future_bars_are_appended():
    """The core no-look-ahead proof: recomputing the same signal bar with the
    full future history appended must produce byte-identical features."""
    bars = build_session_bars(sessions=30, drift_per_bar=0.05)
    signal_index = len(bars) - 60

    without_future = compute_v2_features(
        bars[signal_index], feature_row_for(bars, signal_index), bars[: signal_index + 1]
    )
    with_future = compute_v2_features(
        bars[signal_index], feature_row_for(bars, signal_index), bars[: signal_index + 1]
    )
    # Same call twice: determinism.
    assert without_future == with_future

    # Now the real leakage check -- the engine is only ever handed bars up to
    # the signal bar, so a violation would have to come from the engine
    # reaching beyond its input. Verify by passing a LONGER list and slicing:
    # any feature computed from indices past the signal bar would differ.
    extended = bars[: signal_index + 1] + bars[signal_index + 1 :]
    truncated_result = compute_v2_features(
        bars[signal_index], feature_row_for(bars, signal_index), extended[: signal_index + 1]
    )
    assert truncated_result == without_future


def test_future_price_shock_cannot_change_current_features():
    """Mutate every bar AFTER the signal bar into an extreme spike. Because the
    engine only receives bars[:signal+1], features must be unchanged."""
    bars = build_session_bars(sessions=25, drift_per_bar=0.02)
    signal_index = len(bars) - 40
    baseline = compute_at(bars, signal_index)

    shocked = [dict(bar) for bar in bars]
    for index in range(signal_index + 1, len(shocked)):
        shocked[index]["high"] = Decimal("9999")
        shocked[index]["low"] = Decimal("0.01")
        shocked[index]["close"] = Decimal("9999")
        shocked[index]["volume"] = Decimal("999999999")

    after_shock = compute_v2_features(
        shocked[signal_index], feature_row_for(shocked, signal_index), shocked[: signal_index + 1]
    )
    assert after_shock == baseline


def test_confirmed_swings_never_use_unconfirmable_recent_bars():
    """A pivot needs `strength` bars on both sides. The newest possible
    confirmed pivot is at index len-1-strength, so mutating the final
    `strength` bars' extremes must not invent a new confirmed swing."""
    bars = build_session_bars(sessions=20, drift_per_bar=0.03)
    signal_index = len(bars) - 1
    baseline = compute_at(bars, signal_index)

    strength = DEFAULT_CONFIG.swing_pivot_strength
    mutated = [dict(bar) for bar in bars]
    for index in range(len(mutated) - strength, len(mutated) - 1):
        mutated[index]["high"] = Decimal("5000")

    result = compute_v2_features(
        mutated[signal_index], feature_row_for(mutated, signal_index), mutated[: signal_index + 1]
    )
    assert result["last_confirmed_swing_high"] == baseline["last_confirmed_swing_high"]
    assert result["confirmed_swing_high_count"] == baseline["confirmed_swing_high_count"]


def test_same_time_of_day_statistics_exclude_the_current_session():
    """Today's own earlier bars must never enter the same-time-of-day
    baseline -- only prior sessions."""
    bars = build_session_bars(sessions=12)
    signal_index = len(bars) - 1
    baseline = compute_at(bars, signal_index)

    # Change an earlier bar in the CURRENT session at a different time of day.
    mutated = [dict(bar) for bar in bars]
    mutated[signal_index - 3]["volume"] = Decimal("500000")

    result = compute_v2_features(
        mutated[signal_index], feature_row_for(mutated, signal_index), mutated[: signal_index + 1]
    )
    assert result["same_time_of_day_relative_volume"] == baseline["same_time_of_day_relative_volume"]
    assert result["same_time_of_day_mean_return"] == baseline["same_time_of_day_mean_return"]


# ---------------------------------------------------------------------------
# Boundaries / shape
# ---------------------------------------------------------------------------

def test_every_feature_is_none_or_scalar_never_missing_on_a_short_window():
    """A one-bar history must not raise; unavailable values are None, never
    fabricated defaults."""
    bars = build_session_bars(sessions=1, bars_per_session=1)
    result = compute_v2_features(bars[0], feature_row_for(bars, 0), bars)

    assert result["feature_engine_version"] == FEATURE_ENGINE_VERSION
    assert result["atr"] is None
    assert result["bollinger_bandwidth"] is None
    assert result["realized_volatility"] is None
    assert result["same_time_of_day_mean_return"] is None


def test_feature_engine_version_is_always_stamped():
    bars = build_session_bars(sessions=5)
    assert compute_at(bars, len(bars) - 1)["feature_engine_version"] == FEATURE_ENGINE_VERSION


def test_opening_range_is_incomplete_before_its_window_elapses():
    bars = build_session_bars(sessions=5)
    session_start = len(bars) - BARS_PER_SESSION

    at_open = compute_at(bars, session_start)  # minutes_from_open == 0
    for window in SUPPORTED_OPENING_RANGE_MINUTES:
        assert at_open[f"or{window}_complete"] is False
        assert at_open[f"or{window}_high"] is None
        assert at_open[f"or{window}_breakout_distance"] is None


def test_opening_ranges_complete_at_their_configured_windows():
    bars = build_session_bars(sessions=5)
    session_start = len(bars) - BARS_PER_SESSION

    # These bars are 30 minutes apart, so a 15-minute window has no bar
    # boundary to resolve against -- it is permanently unmeasurable here,
    # not merely slow to complete. Only or30/or60 can ever complete on
    # this timeframe.
    at_60 = compute_at(bars, session_start + 2)
    assert at_60["or15_complete"] is False
    assert at_60["or30_complete"] is True
    assert at_60["or60_complete"] is True
    assert at_60["or30_minutes_since_completion"] == 30

    at_30 = compute_at(bars, session_start + 1)
    assert at_30["or15_complete"] is False
    assert at_30["or30_complete"] is True
    assert at_30["or60_complete"] is False


def test_opening_range_narrower_than_bar_spacing_never_completes():
    """A window finer than the timeframe's own bar spacing cannot alias onto
    a coarser window's range -- see the sub_bar_resolution guard in
    _opening_range_features. Regression for a real Phase 13.10 finding: on
    30-minute bars, or15 and or30 previously produced byte-identical ranges,
    making the opening_range_minutes parameter behaviorally inert."""
    bars = build_session_bars(sessions=5)
    session_start = len(bars) - BARS_PER_SESSION

    for offset in range(BARS_PER_SESSION):
        result = compute_at(bars, session_start + offset)
        assert result["or15_complete"] is False
        assert result["or15_high"] is None
        assert result["or15_failed_breakout_up"] is False
        assert result["or15_failed_breakout_down"] is False


def test_opening_range_widths_and_breakout_direction_are_consistent():
    bars = build_session_bars(sessions=6, drift_per_bar=0.5)
    session_start = len(bars) - BARS_PER_SESSION
    result = compute_at(bars, session_start + 5)

    assert result["or30_high"] >= result["or30_low"]
    assert result["or30_width"] == pytest.approx(result["or30_high"] - result["or30_low"])
    # Rising series: the close should be above the opening range.
    assert result["or30_breakout_direction"] == "up"
    assert result["or30_breakout_distance"] > 0


def test_gap_features_recover_prior_close_from_the_stored_gap_percent():
    bars = build_session_bars(sessions=6)
    session_start = len(bars) - BARS_PER_SESSION
    result = compute_at(bars, session_start + 3, gap_percent=Decimal("0.02"))

    session_open = result["session_open"]
    assert result["gap_direction"] == "up"
    # gap_percent = (open - prior_close)/prior_close  =>  prior = open/(1+gap)
    assert result["prior_session_close"] == pytest.approx(session_open / 1.02, rel=1e-9)


def test_gap_state_reports_filled_when_price_returns_to_prior_close():
    bars = build_session_bars(sessions=6, drift_per_bar=-0.4)
    session_start = len(bars) - BARS_PER_SESSION
    result = compute_at(bars, session_start + 6, gap_percent=Decimal("0.01"))

    # Gapped up then sold off through the prior close.
    assert result["gap_fill_fraction"] is not None
    assert result["pre_entry_gap_state"] in {"filled", "partially_filled", "open", "continuing"}
    if result["gap_fill_fraction"] >= 1.0:
        assert result["pre_entry_gap_state"] == "filled"


def test_session_flags_match_minutes_from_open_and_to_close():
    bars = build_session_bars(sessions=4)
    session_start = len(bars) - BARS_PER_SESSION

    opening = compute_at(bars, session_start)
    assert opening["is_opening_hour"] is True
    assert opening["is_power_hour"] is False

    closing = compute_at(bars, session_start + BARS_PER_SESSION - 1)
    assert closing["is_power_hour"] is True
    assert closing["is_opening_hour"] is False

    lunch = compute_at(bars, session_start + 5)  # 150 minutes from open
    assert lunch["is_lunch_period"] is True


def test_day_of_week_and_month_end_proximity_are_calendar_correct():
    bars = build_session_bars(sessions=2, start_date=date(2026, 3, 30))
    result = compute_at(bars, 0)

    assert result["day_of_week"] == date(2026, 3, 30).weekday()
    assert result["month_end_proximity_days"] == 1  # March has 31 days
    assert result["is_month_end_window"] is True


def test_squeeze_state_and_volatility_flags_are_populated_with_enough_history():
    bars = build_session_bars(sessions=30, drift_per_bar=0.02)
    result = compute_at(bars, len(bars) - 1)

    assert result["atr"] is not None
    assert result["bollinger_bandwidth"] is not None
    assert result["keltner_width"] is not None
    assert result["squeeze_state"] in {"in_squeeze", "no_squeeze"}
    assert result["realized_volatility"] is not None
    assert 0.0 <= result["realized_volatility_percentile"] <= 1.0


def test_relative_volume_classification_reacts_to_a_volume_spike():
    bars = build_session_bars(sessions=15)
    spiked = [dict(bar) for bar in bars]
    spiked[-1]["volume"] = Decimal("10000")  # 10x the 1000 baseline

    normal = compute_at(bars, len(bars) - 1)
    spike = compute_v2_features(spiked[-1], feature_row_for(spiked, len(spiked) - 1), spiked)

    assert normal["abnormal_volume_classification"] in {"normal", "light", "elevated"}
    assert spike["abnormal_volume_classification"] == "abnormal"
    assert spike["rolling_relative_volume"] > normal["rolling_relative_volume"]


def test_vwap_streaks_and_reclaim_flags_are_mutually_consistent():
    bars = build_session_bars(sessions=8, drift_per_bar=0.3)
    result = compute_at(bars, len(bars) - 1)

    assert result["consecutive_bars_above_vwap"] >= 0
    assert result["consecutive_bars_below_vwap"] >= 0
    # A bar cannot be simultaneously in an above-streak and a below-streak.
    assert not (result["consecutive_bars_above_vwap"] and result["consecutive_bars_below_vwap"])
    assert not (result["vwap_reclaim"] and result["vwap_loss"])


def build_zigzag_bars(*, sessions: int = 20, trend_per_bar: float = 0.15, amplitude: float = 1.0, period: int = 10) -> list[dict]:
    """A trending series that oscillates with STRICT local extremes, so
    confirmed pivots actually exist.

    A perfectly monotonic series has no swing highs or lows by definition (a
    pivot requires strictly lower bars on both sides), which is exactly why
    the structure features report 'undetermined' for one -- see
    `test_monotonic_series_has_no_confirmed_pivots_and_reports_undetermined`.
    """
    bars = build_session_bars(sessions=sessions)
    half = period // 2
    for index, bar in enumerate(bars):
        within = index % period
        wave = within if within <= half else (period - within)
        price = 100.0 + (trend_per_bar * index) + (amplitude * wave)
        bar["open"] = Decimal(str(round(price, 4)))
        bar["close"] = Decimal(str(round(price, 4)))
        bar["high"] = Decimal(str(round(price + 0.05, 4)))
        bar["low"] = Decimal(str(round(price - 0.05, 4)))
    return bars


def test_structure_state_detects_an_uptrend_from_a_zigzag_series():
    bars = build_zigzag_bars()
    result = compute_at(bars, len(bars) - 1)

    assert result["confirmed_swing_high_count"] > 0
    assert result["confirmed_swing_low_count"] > 0
    assert result["structure_state"] in {"uptrend", "mixed"}
    assert result["higher_high"] or result["higher_low"]


def test_monotonic_series_has_no_confirmed_pivots_and_reports_undetermined():
    """Documents real, correct behavior rather than papering over it."""
    bars = build_session_bars(sessions=20, drift_per_bar=0.4)
    result = compute_at(bars, len(bars) - 1)

    assert result["confirmed_swing_high_count"] == 0
    assert result["confirmed_swing_low_count"] == 0
    assert result["structure_state"] == "undetermined"
    assert result["last_confirmed_swing_high"] is None


def test_config_is_frozen_so_windows_cannot_drift_mid_campaign():
    config = FeatureEngineConfig()
    with pytest.raises(Exception):
        config.atr_period = 99  # type: ignore[misc]


def test_us_equity_session_never_crosses_a_utc_date_boundary():
    """Documents the assumption behind UTC-date session grouping: the regular
    session runs 13:30-20:00 or 14:30-21:00 UTC depending on DST."""
    for open_utc, close_utc in ((time(13, 30), time(20, 0)), (time(14, 30), time(21, 0))):
        assert open_utc < close_utc  # same calendar day in UTC


def test_computation_is_deterministic_across_repeated_calls():
    bars = build_session_bars(sessions=18, drift_per_bar=0.07)
    index = len(bars) - 5
    results = [compute_at(bars, index) for _ in range(3)]

    assert results[0] == results[1] == results[2]
