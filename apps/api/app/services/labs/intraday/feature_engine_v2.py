"""Phase 13.2: Feature Engine V2 -- pre-entry features for Strategy Engine V2.

Computed at decide time from exactly two inputs, both of which the simulator
already hands every strategy:

  * `feature`      -- the current bar's `intraday_features` row (authoritative
                      session-aware fields: session_vwap, opening_range_*,
                      gap_percent, session_relative_volume, minutes_from_open,
                      minutes_to_close, session_date). Computed upstream by
                      `labs/intraday/features.py` with correct exchange-session
                      and timezone handling; V2 never re-derives them.
  * `recent_candles` -- bars up to AND INCLUDING the current (signal) bar, and
                      never beyond it. See `backtester.run_backtest`, which
                      slices `candle_rows[recent_start : i + 1]`.

**No-look-ahead invariant.** Every value below is a function of
`recent_candles[:len(recent_candles)]` and the current bar's own feature row.
The current bar is included because it has closed by decision time and the
entry fills at the *next* bar's open -- the same convention every existing
family already uses. Appending any future bar to `recent_candles` must not
change a single output; `test_feature_engine_v2.py` asserts exactly that for
every feature key.

**Reproducibility.** No wall-clock reads, no RNG, no database access, no
global state. Given identical inputs the output is byte-identical, so frozen
dataset snapshots reproduce features exactly. `FEATURE_ENGINE_VERSION` is
stamped into every result and into candidate parameters, so a stored result
always names the feature vocabulary that produced it.

**Session grouping.** Prior-session bars are identified by UTC calendar date.
For US equities the regular session spans 13:30-20:00 or 14:30-21:00 UTC
depending on DST and therefore never crosses a UTC date boundary, so UTC date
is a sound session key here. Half days share the same open, so time-of-day
alignment is unaffected. This assumption is asserted in the tests.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import fmean, pstdev
from typing import Any

FEATURE_ENGINE_VERSION = "intraday_feature_engine_v2"


@dataclass(frozen=True)
class FeatureEngineConfig:
    """All windows are bar counts unless named otherwise. Frozen so a config
    cannot be mutated mid-campaign; the values are folded into candidate
    parameters, so a stored candidate always names the windows used."""

    lookback_bars: int = 400
    atr_period: int = 14
    bollinger_period: int = 20
    bollinger_stdev: float = 2.0
    keltner_period: int = 20
    keltner_atr_multiple: float = 1.5
    realized_volatility_period: int = 20
    realized_volatility_percentile_lookback: int = 200
    volatility_compression_lookback: int = 100
    swing_pivot_strength: int = 2
    structure_swings_tracked: int = 6
    volume_average_period: int = 20
    volume_acceleration_period: int = 5
    same_time_of_day_minimum_samples: int = 5
    same_time_of_day_maximum_samples: int = 40
    vwap_slope_period: int = 5
    vwap_streak_maximum: int = 50
    abnormal_volume_threshold: float = 2.0
    elevated_volume_threshold: float = 1.5


DEFAULT_CONFIG = FeatureEngineConfig()

LUNCH_START_MINUTES_FROM_OPEN = 120  # 11:30 ET
LUNCH_END_MINUTES_FROM_OPEN = 210  # 13:00 ET
OPENING_HOUR_MINUTES = 60
POWER_HOUR_MINUTES_TO_CLOSE = 60
MONTH_END_PROXIMITY_DAYS = 3

SUPPORTED_OPENING_RANGE_MINUTES = (15, 30, 60)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _closes(bars: list[dict[str, Any]]) -> list[float]:
    return [float(bar["close"]) for bar in bars]


def _true_ranges(bars: list[dict[str, Any]]) -> list[float]:
    ranges = []
    for index in range(1, len(bars)):
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        previous_close = float(bars[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return ranges


def _atr(bars: list[dict[str, Any]], period: int) -> float | None:
    ranges = _true_ranges(bars)
    if len(ranges) < period:
        return None
    window = ranges[-period:]
    return fmean(window)


def _session_key(bar: dict[str, Any]) -> Any:
    timestamp = bar["timestamp"]
    return timestamp.date() if isinstance(timestamp, datetime) else timestamp


def _time_of_day(bar: dict[str, Any]) -> Any:
    timestamp = bar["timestamp"]
    return timestamp.time() if isinstance(timestamp, datetime) else None


FEATURE_GROUPS = (
    "session",
    "vwap",
    "opening_range",
    "gap",
    "relative_volume",
    "volatility",
    "market_structure",
    "microstructure",
)


def compute_v2_features(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    *,
    config: FeatureEngineConfig = DEFAULT_CONFIG,
    groups: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return the V2 feature dictionary for the current (signal) bar.

    `groups` restricts computation to the named feature groups. This is a
    pure performance control -- a group's values are identical whether or not
    other groups were requested -- and it matters: the market-structure pivot
    scan and the same-time-of-day baselines are the expensive passes, and
    most families never read them. A family declares the groups it actually
    uses, so a campaign never pays for features it ignores.

    Missing inputs yield None for the affected keys rather than a fabricated
    default -- a strategy that requires a feature must check for None, and a
    None here is honest evidence that the window was too short.
    """

    selected = set(groups) if groups else set(FEATURE_GROUPS)
    unknown = selected - set(FEATURE_GROUPS)
    if unknown:
        raise ValueError(f"Unknown feature group(s): {sorted(unknown)}. Known: {FEATURE_GROUPS}")

    bars = recent_candles[-config.lookback_bars :] if config.lookback_bars else list(recent_candles)
    if not bars:
        bars = [candle]

    result: dict[str, Any] = {"feature_engine_version": FEATURE_ENGINE_VERSION}
    if "session" in selected:
        result.update(_session_features(candle, feature, bars, config))
    if "vwap" in selected:
        result.update(_vwap_features(candle, feature, bars, config))
    if "opening_range" in selected:
        result.update(_opening_range_features(candle, feature, bars, config))
    if "gap" in selected:
        result.update(_gap_features(candle, feature, bars, config))
    if "relative_volume" in selected:
        result.update(_relative_volume_features(candle, feature, bars, config))
    if "volatility" in selected:
        result.update(_volatility_features(candle, feature, bars, config))
    if "market_structure" in selected:
        result.update(_market_structure_features(candle, feature, bars, config))
    if "microstructure" in selected:
        result.update(
            {
                "quote_count": feature.get("quote_count"),
                "median_spread_bps": _f(feature.get("median_spread_bps")),
                "p90_spread_bps": _f(feature.get("p90_spread_bps")),
                "mean_depth": _f(feature.get("mean_depth")),
                "order_flow_imbalance": _f(feature.get("order_flow_imbalance")),
                "normalized_order_flow_imbalance": _f(
                    feature.get("normalized_order_flow_imbalance")
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Session / seasonality
# ---------------------------------------------------------------------------

def _session_features(candle, feature, bars, config) -> dict[str, Any]:
    minutes_from_open = feature.get("minutes_from_open")
    minutes_to_close = feature.get("minutes_to_close")
    timestamp = candle.get("timestamp")

    same_time_returns: list[float] = []
    same_time_ranges: list[float] = []
    next_same_time_returns: list[float] = []
    current_time = _time_of_day(candle)
    current_session = _session_key(candle)
    if current_time is not None:
        # Bars at the same wall-clock time on PRIOR sessions only; the current
        # session is excluded so today's own path can never inform the feature.
        for index in range(1, len(bars)):
            bar = bars[index]
            if _session_key(bar) == current_session or _time_of_day(bar) != current_time:
                continue
            previous_close = float(bars[index - 1]["close"])
            if previous_close <= 0:
                continue
            same_time_returns.append((float(bar["close"]) - previous_close) / previous_close)
            high, low = float(bar["high"]), float(bar["low"])
            if previous_close > 0:
                same_time_ranges.append((high - low) / previous_close)
        same_time_returns = same_time_returns[-config.same_time_of_day_maximum_samples :]
        same_time_ranges = same_time_ranges[-config.same_time_of_day_maximum_samples :]

    current_session_bars = [bar for bar in bars if _session_key(bar) == current_session]
    bar_minutes = _typical_bar_minutes(current_session_bars)
    next_time = None
    if isinstance(timestamp, datetime) and bar_minutes:
        next_time = (timestamp + timedelta(minutes=bar_minutes)).timetz().replace(tzinfo=None)
        for index in range(1, len(bars)):
            bar = bars[index]
            if _session_key(bar) == current_session or _time_of_day(bar) != next_time:
                continue
            open_price = float(bar["open"])
            if open_price <= 0:
                continue
            next_same_time_returns.append((float(bar["close"]) - open_price) / open_price)
        next_same_time_returns = next_same_time_returns[-config.same_time_of_day_maximum_samples :]

    enough = len(same_time_returns) >= config.same_time_of_day_minimum_samples
    first_half_hour_return = None
    gap_percent = _f(feature.get("gap_percent"))
    if current_session_bars and gap_percent is not None and (1 + gap_percent) != 0:
        session_open = float(current_session_bars[0]["open"])
        prior_close = session_open / (1 + gap_percent)
        if prior_close > 0:
            first_half_hour_return = (
                float(current_session_bars[0]["close"]) - prior_close
            ) / prior_close

    month_end_proximity_days = None
    if isinstance(timestamp, datetime):
        days_in_month = monthrange(timestamp.year, timestamp.month)[1]
        month_end_proximity_days = max(0, days_in_month - timestamp.day)

    return {
        "minutes_from_open": minutes_from_open,
        "minutes_to_close": minutes_to_close,
        "is_opening_hour": (minutes_from_open is not None and minutes_from_open < OPENING_HOUR_MINUTES),
        "is_lunch_period": (
            minutes_from_open is not None
            and LUNCH_START_MINUTES_FROM_OPEN <= minutes_from_open < LUNCH_END_MINUTES_FROM_OPEN
        ),
        "is_power_hour": (minutes_to_close is not None and minutes_to_close <= POWER_HOUR_MINUTES_TO_CLOSE),
        "day_of_week": timestamp.weekday() if isinstance(timestamp, datetime) else None,
        "month_end_proximity_days": month_end_proximity_days,
        "is_month_end_window": (month_end_proximity_days is not None and month_end_proximity_days <= MONTH_END_PROXIMITY_DAYS),
        "same_time_of_day_sample_count": len(same_time_returns),
        "same_time_of_day_mean_return": round(fmean(same_time_returns), 8) if enough else None,
        "same_time_of_day_return_volatility": round(pstdev(same_time_returns), 8) if enough and len(same_time_returns) > 1 else None,
        "same_time_of_day_mean_range": round(fmean(same_time_ranges), 8) if enough and same_time_ranges else None,
        "next_same_time_of_day_sample_count": len(next_same_time_returns),
        "next_same_time_of_day_mean_return": (
            round(fmean(next_same_time_returns), 8)
            if len(next_same_time_returns) >= config.same_time_of_day_minimum_samples
            else None
        ),
        "first_half_hour_return_from_prior_close": (
            round(first_half_hour_return, 8)
            if first_half_hour_return is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def _vwap_features(candle, feature, bars, config) -> dict[str, Any]:
    session_vwap = _f(feature.get("session_vwap"))
    distance = _f(feature.get("distance_from_session_vwap"))
    close = float(candle["close"])
    atr = _atr(bars, config.atr_period)

    current_session = _session_key(candle)
    session_bars = [bar for bar in bars if _session_key(bar) == current_session]

    # VWAP slope proxy: change in (close - vwap) is unavailable historically
    # because only the current bar carries a feature row. Instead reconstruct
    # this session's running VWAP from this session's own bars -- identical
    # definition to features.py (cumulative typical-price * volume / volume),
    # and strictly backward-looking within the session.
    running_vwap: list[float] = []
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    for bar in session_bars:
        high, low, bar_close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        typical = (high + low + bar_close) / 3.0
        volume = float(bar["volume"])
        cumulative_pv += typical * volume
        cumulative_volume += volume
        running_vwap.append(cumulative_pv / cumulative_volume if cumulative_volume > 0 else bar_close)

    vwap_slope = None
    if len(running_vwap) > config.vwap_slope_period:
        past = running_vwap[-1 - config.vwap_slope_period]
        if past > 0:
            vwap_slope = (running_vwap[-1] - past) / past

    above_below = [1 if float(bar["close"]) > vwap else (-1 if float(bar["close"]) < vwap else 0) for bar, vwap in zip(session_bars, running_vwap)]
    streak = 0
    if above_below and above_below[-1] != 0:
        sign = above_below[-1]
        for value in reversed(above_below):
            if value != sign:
                break
            streak += 1
        streak = min(streak, config.vwap_streak_maximum)
    consecutive_above = streak if above_below and above_below[-1] > 0 else 0
    consecutive_below = streak if above_below and above_below[-1] < 0 else 0

    # Reclaim: was below VWAP on the previous bar, is above now (and vice versa
    # for rejection: touched/exceeded VWAP intrabar but closed back below).
    vwap_reclaim = bool(len(above_below) >= 2 and above_below[-2] < 0 and above_below[-1] > 0)
    vwap_loss = bool(len(above_below) >= 2 and above_below[-2] > 0 and above_below[-1] < 0)
    vwap_rejection_from_above = bool(
        running_vwap and float(candle["high"]) >= running_vwap[-1] and close < running_vwap[-1]
    )
    vwap_rejection_from_below = bool(
        running_vwap and float(candle["low"]) <= running_vwap[-1] and close > running_vwap[-1]
    )

    deviation_atr = None
    if session_vwap is not None and atr and atr > 0:
        deviation_atr = (close - session_vwap) / atr

    return {
        "session_vwap": session_vwap,
        "distance_from_session_vwap": distance,
        "vwap_slope": round(vwap_slope, 8) if vwap_slope is not None else None,
        "vwap_reclaim": vwap_reclaim,
        "vwap_loss": vwap_loss,
        "vwap_rejection_from_above": vwap_rejection_from_above,
        "vwap_rejection_from_below": vwap_rejection_from_below,
        "consecutive_bars_above_vwap": consecutive_above,
        "consecutive_bars_below_vwap": consecutive_below,
        "vwap_deviation_atr": round(deviation_atr, 8) if deviation_atr is not None else None,
    }


# ---------------------------------------------------------------------------
# Opening range
# ---------------------------------------------------------------------------

def _opening_range_features(candle, feature, bars, config) -> dict[str, Any]:
    close = float(candle["close"])
    minutes_from_open = feature.get("minutes_from_open")
    stored_high = _f(feature.get("opening_range_high"))
    stored_low = _f(feature.get("opening_range_low"))
    stored_minutes = feature.get("opening_range_minutes")
    atr = _atr(bars, config.atr_period)

    current_session = _session_key(candle)
    session_bars = [bar for bar in bars if _session_key(bar) == current_session]
    bar_minutes = _typical_bar_minutes(session_bars)

    result: dict[str, Any] = {
        "opening_range_high": stored_high,
        "opening_range_low": stored_low,
        "opening_range_minutes": int(stored_minutes) if stored_minutes is not None else None,
        "opening_range_position": _f(feature.get("opening_range_position")),
    }

    # Configurable 15/30/60-minute ranges, reconstructed from this session's
    # own bars. A range is only "complete" once the clock has passed its
    # window -- an incomplete range reports complete=False and no levels, so a
    # strategy can never trade a range that has not finished forming.
    #
    # A window narrower than the timeframe's own bar spacing cannot be
    # measured at all -- there is no bar boundary at that resolution. Without
    # this guard such a window silently collapses onto the same single first
    # bar as the next coarser window (e.g. a 15-minute range on 30-minute
    # bars), making the two indistinguishable and the window parameter inert.
    # Reported as permanently incomplete instead, so a strategy configured
    # for it correctly refuses every bar rather than quietly duplicating a
    # coarser window's signal.
    for window in SUPPORTED_OPENING_RANGE_MINUTES:
        sub_bar_resolution = bar_minutes is not None and window < bar_minutes
        prefix = f"or{window}"
        window_bars = []
        elapsed_known = minutes_from_open is not None
        if sub_bar_resolution:
            result[f"{prefix}_complete"] = False
            result[f"{prefix}_high"] = None
            result[f"{prefix}_low"] = None
            result[f"{prefix}_width"] = None
            result[f"{prefix}_width_atr"] = None
            result[f"{prefix}_volume"] = None
            result[f"{prefix}_minutes_since_completion"] = None
            result[f"{prefix}_breakout_distance"] = None
            result[f"{prefix}_breakout_direction"] = None
            result[f"{prefix}_failed_breakout_up"] = False
            result[f"{prefix}_failed_breakout_down"] = False
            continue
        for bar in session_bars:
            bar_minutes = _minutes_from_open_for(bar, session_bars)
            if bar_minutes is None or bar_minutes >= window:
                continue
            window_bars.append(bar)
        complete = bool(elapsed_known and minutes_from_open >= window and window_bars)
        high = max((float(bar["high"]) for bar in window_bars), default=None) if window_bars else None
        low = min((float(bar["low"]) for bar in window_bars), default=None) if window_bars else None
        volume = sum(float(bar["volume"]) for bar in window_bars) if window_bars else None
        width = (high - low) if (complete and high is not None and low is not None) else None

        result[f"{prefix}_complete"] = complete
        result[f"{prefix}_high"] = high if complete else None
        result[f"{prefix}_low"] = low if complete else None
        result[f"{prefix}_width"] = width
        result[f"{prefix}_width_atr"] = round(width / atr, 8) if (width is not None and atr and atr > 0) else None
        result[f"{prefix}_volume"] = volume if complete else None
        result[f"{prefix}_minutes_since_completion"] = (
            max(0, minutes_from_open - window) if (complete and minutes_from_open is not None) else None
        )
        if complete and high is not None and low is not None:
            above = close - high
            below = low - close
            result[f"{prefix}_breakout_distance"] = round(above if above > 0 else (-below if below > 0 else 0.0), 8)
            result[f"{prefix}_breakout_direction"] = "up" if above > 0 else ("down" if below > 0 else "inside")
            # Failed breakout: this session traded beyond the range at some
            # point after completion, but the current bar closed back inside.
            broke_up = any(float(bar["high"]) > high for bar in _bars_after(session_bars, window))
            broke_down = any(float(bar["low"]) < low for bar in _bars_after(session_bars, window))
            inside_now = low <= close <= high
            result[f"{prefix}_failed_breakout_up"] = bool(broke_up and inside_now)
            result[f"{prefix}_failed_breakout_down"] = bool(broke_down and inside_now)
        else:
            result[f"{prefix}_breakout_distance"] = None
            result[f"{prefix}_breakout_direction"] = None
            result[f"{prefix}_failed_breakout_up"] = False
            result[f"{prefix}_failed_breakout_down"] = False
    return result


def _typical_bar_minutes(session_bars: list[dict[str, Any]]) -> int | None:
    """The spacing between consecutive bars, from real timestamps rather
    than an assumed timeframe string, so it reflects what the data can
    actually resolve. Uses the first gap found scanning from the most
    recent pair backward, skipping any zero-length gap from a duplicate
    timestamp."""
    for later, earlier in zip(reversed(session_bars), reversed(session_bars[:-1])):
        a, b = earlier.get("timestamp"), later.get("timestamp")
        if isinstance(a, datetime) and isinstance(b, datetime):
            delta = int((b - a).total_seconds() // 60)
            if delta > 0:
                return delta
    return None


def _minutes_from_open_for(bar: dict[str, Any], session_bars: list[dict[str, Any]]) -> int | None:
    """Minutes between a bar and its session's first bar. Derived from bar
    timestamps within the session, so it needs no per-bar feature row."""
    if not session_bars:
        return None
    first = session_bars[0]["timestamp"]
    current = bar["timestamp"]
    if not isinstance(first, datetime) or not isinstance(current, datetime):
        return None
    return int((current - first).total_seconds() // 60)


def _bars_after(session_bars: list[dict[str, Any]], window_minutes: int) -> list[dict[str, Any]]:
    out = []
    for bar in session_bars:
        minutes = _minutes_from_open_for(bar, session_bars)
        if minutes is not None and minutes >= window_minutes:
            out.append(bar)
    return out


# ---------------------------------------------------------------------------
# Gap
# ---------------------------------------------------------------------------

def _gap_features(candle, feature, bars, config) -> dict[str, Any]:
    gap_percent = _f(feature.get("gap_percent"))
    close = float(candle["close"])
    atr = _atr(bars, config.atr_period)

    current_session = _session_key(candle)
    session_bars = [bar for bar in bars if _session_key(bar) == current_session]
    session_open = float(session_bars[0]["open"]) if session_bars else None

    prior_close = None
    if gap_percent is not None and session_open is not None and (1.0 + gap_percent) != 0:
        # features.py defines gap_percent = (session_open - prior_close) / prior_close,
        # so prior_close is recoverable exactly rather than re-derived from a
        # possibly-different session boundary rule.
        prior_close = session_open / (1.0 + gap_percent)

    direction = None
    if gap_percent is not None:
        direction = "up" if gap_percent > 0 else ("down" if gap_percent < 0 else "flat")

    gap_atr = None
    if gap_percent is not None and prior_close is not None and atr and atr > 0:
        gap_atr = (session_open - prior_close) / atr if session_open is not None else None

    # Fill fraction: how much of the opening gap has been retraced by the
    # current close. 0 = no retrace, 1 = fully filled back to prior close.
    fill_fraction = None
    if prior_close is not None and session_open is not None and session_open != prior_close:
        fill_fraction = (session_open - close) / (session_open - prior_close)
        fill_fraction = round(max(0.0, min(2.0, fill_fraction)), 8)

    session_extreme_beyond_open = None
    if session_bars and direction in ("up", "down"):
        if direction == "up":
            session_extreme_beyond_open = max(float(bar["high"]) for bar in session_bars) - (session_open or 0.0)
        else:
            session_extreme_beyond_open = (session_open or 0.0) - min(float(bar["low"]) for bar in session_bars)

    continuation = bool(
        direction == "up" and session_open is not None and close > session_open
    ) or bool(direction == "down" and session_open is not None and close < session_open)

    if fill_fraction is None:
        gap_state = "no_gap" if gap_percent in (None, 0.0) else "unknown"
    elif fill_fraction >= 1.0:
        gap_state = "filled"
    elif fill_fraction >= 0.5:
        gap_state = "partially_filled"
    elif continuation:
        gap_state = "continuing"
    else:
        gap_state = "open"

    return {
        "gap_percent": gap_percent,
        "gap_direction": direction,
        "gap_atr": round(gap_atr, 8) if gap_atr is not None else None,
        "prior_session_close": round(prior_close, 8) if prior_close is not None else None,
        "session_open": session_open,
        "gap_fill_fraction": fill_fraction,
        "gap_continuation": continuation,
        "gap_extension_beyond_open": round(session_extreme_beyond_open, 8) if session_extreme_beyond_open is not None else None,
        "pre_entry_gap_state": gap_state,
        "close_above_prior_close": bool(prior_close is not None and close > prior_close),
    }


# ---------------------------------------------------------------------------
# Relative volume
# ---------------------------------------------------------------------------

def _relative_volume_features(candle, feature, bars, config) -> dict[str, Any]:
    volume = float(candle["volume"])
    session_relative_volume = _f(feature.get("session_relative_volume"))

    history = [float(bar["volume"]) for bar in bars[:-1]]
    rolling = history[-config.volume_average_period :]
    rolling_average = fmean(rolling) if rolling else None
    rolling_relative_volume = (volume / rolling_average) if (rolling_average and rolling_average > 0) else None

    recent = history[-config.volume_acceleration_period :]
    recent_average = fmean(recent) if recent else None
    volume_acceleration = (volume / recent_average) if (recent_average and recent_average > 0) else None

    # Same-time-of-day volume across PRIOR sessions only.
    current_time = _time_of_day(candle)
    current_session = _session_key(candle)
    same_time_volumes = [
        float(bar["volume"])
        for bar in bars
        if _session_key(bar) != current_session and _time_of_day(bar) == current_time
    ][-config.same_time_of_day_maximum_samples :]
    same_time_average = fmean(same_time_volumes) if same_time_volumes else None
    same_time_relative_volume = (
        volume / same_time_average if (same_time_average and same_time_average > 0) else None
    )
    same_time_reliable = len(same_time_volumes) >= config.same_time_of_day_minimum_samples

    reference = same_time_relative_volume if (same_time_reliable and same_time_relative_volume is not None) else rolling_relative_volume
    if reference is None:
        classification = "unknown"
    elif reference >= config.abnormal_volume_threshold:
        classification = "abnormal"
    elif reference >= config.elevated_volume_threshold:
        classification = "elevated"
    elif reference >= 0.7:
        classification = "normal"
    else:
        classification = "light"

    spike_persistence = 0
    if rolling_average and rolling_average > 0:
        for bar in reversed(bars):
            if float(bar["volume"]) / rolling_average >= config.elevated_volume_threshold:
                spike_persistence += 1
            else:
                break

    previous_close = float(bars[-2]["close"]) if len(bars) >= 2 else None
    bar_return = ((float(candle["close"]) - previous_close) / previous_close) if (previous_close and previous_close > 0) else None
    price_volume_confirmation = bool(
        bar_return is not None
        and reference is not None
        and reference >= config.elevated_volume_threshold
        and abs(bar_return) > 0
    )

    return {
        "session_relative_volume": session_relative_volume,
        "rolling_relative_volume": round(rolling_relative_volume, 8) if rolling_relative_volume is not None else None,
        "same_time_of_day_relative_volume": round(same_time_relative_volume, 8) if same_time_relative_volume is not None else None,
        "same_time_of_day_volume_samples": len(same_time_volumes),
        "same_time_of_day_volume_reliable": same_time_reliable,
        "volume_acceleration": round(volume_acceleration, 8) if volume_acceleration is not None else None,
        "volume_spike_persistence_bars": spike_persistence,
        "price_volume_confirmation": price_volume_confirmation,
        "abnormal_volume_classification": classification,
        "bar_return": round(bar_return, 8) if bar_return is not None else None,
    }


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------

def _volatility_features(candle, feature, bars, config) -> dict[str, Any]:
    closes = _closes(bars)
    close = closes[-1]
    atr = _atr(bars, config.atr_period)

    bollinger_bandwidth = None
    if len(closes) >= config.bollinger_period:
        window = closes[-config.bollinger_period :]
        mean = fmean(window)
        deviation = pstdev(window) if len(window) > 1 else 0.0
        if mean > 0:
            bollinger_bandwidth = (2 * config.bollinger_stdev * deviation) / mean

    keltner_width = None
    if atr is not None and len(closes) >= config.keltner_period:
        mean = fmean(closes[-config.keltner_period :])
        if mean > 0:
            keltner_width = (2 * config.keltner_atr_multiple * atr) / mean

    squeeze_state = None
    if bollinger_bandwidth is not None and keltner_width is not None:
        squeeze_state = "in_squeeze" if bollinger_bandwidth < keltner_width else "no_squeeze"

    returns = []
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        if previous > 0:
            returns.append((closes[index] - previous) / previous)

    realized_volatility = None
    if len(returns) >= config.realized_volatility_period:
        realized_volatility = pstdev(returns[-config.realized_volatility_period :])

    realized_volatility_percentile = None
    if realized_volatility is not None and len(returns) >= config.realized_volatility_period * 2:
        history = []
        span = min(len(returns), config.realized_volatility_percentile_lookback)
        for end in range(config.realized_volatility_period, span + 1):
            window = returns[end - config.realized_volatility_period : end]
            history.append(pstdev(window))
        if history:
            below = sum(1 for value in history if value <= realized_volatility)
            realized_volatility_percentile = round(below / len(history), 8)

    atr_compression_ratio = None
    if atr is not None:
        ranges = _true_ranges(bars)
        span = min(len(ranges), config.volatility_compression_lookback)
        if span >= config.atr_period * 2:
            long_average = fmean(ranges[-span:])
            if long_average > 0:
                atr_compression_ratio = round(atr / long_average, 8)

    volatility_expansion = bool(atr_compression_ratio is not None and atr_compression_ratio > 1.2)
    volatility_compression = bool(atr_compression_ratio is not None and atr_compression_ratio < 0.8)

    current_range = float(candle["high"]) - float(candle["low"])
    range_expansion_ratio = None
    ranges = _true_ranges(bars)
    if len(ranges) > config.atr_period:
        baseline = fmean(ranges[-config.atr_period - 1 : -1])
        if baseline > 0:
            range_expansion_ratio = round(current_range / baseline, 8)

    # Compression measured over the window ending BEFORE the current bar.
    # A large expansion bar mechanically inflates ATR and the Bollinger
    # stdev, destroying the very compression reading it is supposed to
    # follow -- so "was this coiled before it moved?" can only be answered
    # from the prior window. Strictly more backward-looking than the
    # current-bar values, never less.
    prior_squeeze_state = None
    prior_compression_ratio = None
    if len(bars) > 1:
        prior = _volatility_compression_only(bars[:-1], config)
        prior_squeeze_state = prior["squeeze_state"]
        prior_compression_ratio = prior["atr_compression_ratio"]

    return {
        "atr": round(atr, 8) if atr is not None else None,
        "atr_percent_of_price": round(atr / close, 8) if (atr is not None and close > 0) else None,
        "bollinger_bandwidth": round(bollinger_bandwidth, 8) if bollinger_bandwidth is not None else None,
        "keltner_width": round(keltner_width, 8) if keltner_width is not None else None,
        "squeeze_state": squeeze_state,
        "prior_squeeze_state": prior_squeeze_state,
        "prior_atr_compression_ratio": prior_compression_ratio,
        "realized_volatility": round(realized_volatility, 8) if realized_volatility is not None else None,
        "realized_volatility_percentile": realized_volatility_percentile,
        "atr_compression_ratio": atr_compression_ratio,
        "volatility_expansion": volatility_expansion,
        "volatility_compression": volatility_compression,
        "range_expansion_ratio": range_expansion_ratio,
    }


def _volatility_compression_only(bars, config) -> dict[str, Any]:
    """Just the two compression readings, for the prior-window measurement.
    Shares the exact formulas above so the prior and current values are
    directly comparable."""
    closes = _closes(bars)
    atr = _atr(bars, config.atr_period)

    bollinger_bandwidth = None
    if len(closes) >= config.bollinger_period:
        window = closes[-config.bollinger_period :]
        mean = fmean(window)
        deviation = pstdev(window) if len(window) > 1 else 0.0
        if mean > 0:
            bollinger_bandwidth = (2 * config.bollinger_stdev * deviation) / mean

    keltner_width = None
    if atr is not None and len(closes) >= config.keltner_period:
        mean = fmean(closes[-config.keltner_period :])
        if mean > 0:
            keltner_width = (2 * config.keltner_atr_multiple * atr) / mean

    squeeze_state = None
    if bollinger_bandwidth is not None and keltner_width is not None:
        squeeze_state = "in_squeeze" if bollinger_bandwidth < keltner_width else "no_squeeze"

    compression_ratio = None
    if atr is not None:
        ranges = _true_ranges(bars)
        span = min(len(ranges), config.volatility_compression_lookback)
        if span >= config.atr_period * 2:
            long_average = fmean(ranges[-span:])
            if long_average > 0:
                compression_ratio = round(atr / long_average, 8)

    return {"squeeze_state": squeeze_state, "atr_compression_ratio": compression_ratio}


# ---------------------------------------------------------------------------
# Market structure
# ---------------------------------------------------------------------------

def _market_structure_features(candle, feature, bars, config) -> dict[str, Any]:
    strength = config.swing_pivot_strength
    close = float(candle["close"])

    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    # A pivot is only CONFIRMED once `strength` bars have printed after it, so
    # the newest confirmable pivot sits at index len(bars) - 1 - strength.
    # Nothing here can see a bar the strategy has not already seen.
    for index in range(strength, len(bars) - strength):
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        left = bars[index - strength : index]
        right = bars[index + 1 : index + 1 + strength]
        if all(high > float(bar["high"]) for bar in left) and all(high > float(bar["high"]) for bar in right):
            swing_highs.append((index, high))
        if all(low < float(bar["low"]) for bar in left) and all(low < float(bar["low"]) for bar in right):
            swing_lows.append((index, low))

    tracked = config.structure_swings_tracked
    swing_highs = swing_highs[-tracked:]
    swing_lows = swing_lows[-tracked:]

    last_swing_high = swing_highs[-1][1] if swing_highs else None
    last_swing_low = swing_lows[-1][1] if swing_lows else None
    higher_high = bool(len(swing_highs) >= 2 and swing_highs[-1][1] > swing_highs[-2][1])
    higher_low = bool(len(swing_lows) >= 2 and swing_lows[-1][1] > swing_lows[-2][1])
    lower_high = bool(len(swing_highs) >= 2 and swing_highs[-1][1] < swing_highs[-2][1])
    lower_low = bool(len(swing_lows) >= 2 and swing_lows[-1][1] < swing_lows[-2][1])

    if higher_high and higher_low:
        structure_state = "uptrend"
    elif lower_high and lower_low:
        structure_state = "downtrend"
    elif swing_highs and swing_lows:
        structure_state = "mixed"
    else:
        structure_state = "undetermined"

    structure_break_up = bool(last_swing_high is not None and close > last_swing_high)
    structure_break_down = bool(last_swing_low is not None and close < last_swing_low)

    # Failed break / liquidity-sweep proxy: this bar traded through the level
    # intrabar but closed back on the original side. Bar-data-only proxy --
    # it does not claim to observe real order-book liquidity.
    high = float(candle["high"])
    low = float(candle["low"])
    failed_break_up = bool(last_swing_high is not None and high > last_swing_high and close <= last_swing_high)
    failed_break_down = bool(last_swing_low is not None and low < last_swing_low and close >= last_swing_low)

    return {
        "last_confirmed_swing_high": round(last_swing_high, 8) if last_swing_high is not None else None,
        "last_confirmed_swing_low": round(last_swing_low, 8) if last_swing_low is not None else None,
        "confirmed_swing_high_count": len(swing_highs),
        "confirmed_swing_low_count": len(swing_lows),
        "higher_high": higher_high,
        "higher_low": higher_low,
        "lower_high": lower_high,
        "lower_low": lower_low,
        "structure_state": structure_state,
        "structure_break_up": structure_break_up,
        "structure_break_down": structure_break_down,
        "failed_structure_break_up": failed_break_up,
        "failed_structure_break_down": failed_break_down,
        "liquidity_sweep_up_proxy": failed_break_up,
        "liquidity_sweep_down_proxy": failed_break_down,
        "distance_to_swing_high": round(last_swing_high - close, 8) if last_swing_high is not None else None,
        "distance_to_swing_low": round(close - last_swing_low, 8) if last_swing_low is not None else None,
    }
