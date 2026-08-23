"""Frozen daily signal logic for the three established paper strategies.

This module is deliberately pure: it accepts completed daily bars and returns a
decision.  Broker access, position ownership and order submission live in the
execution service.  Keeping the research rule here makes it possible to prove
that execution is evaluating the same rule without giving research code a
broker client.

The definitions come from the forward artifacts recovered from the production
host. The RSI entry/exit levels are the deployment reconstruction compatible
with the recorded 2026-08-20 signal (the archive did not contain its generator):

* ``SPY_RSI5_SMA200_V1`` -- close above SMA200, Wilder RSI(5) below 35;
  recover at RSI(5) above 50.  Decisions execute at the next regular-session
  open.
* ``SPY_CONNORS_PULLBACK_V1`` -- Connors RSI(3,2,100) below 10 while SPY is
  above SMA200; enter next open, protect at entry minus 3 x signal ATR(14), and
  exit next open after a completed close above SMA5.
* ``MOM_12_1`` -- the monthly portfolio selection itself is represented by
  :func:`rank_mom_12_1`: Close[t-21] / Close[t-252] - 1, top decile,
  equal-dollar weighted.

No function in this file can submit, cancel or otherwise mutate a broker order.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

RSI5_STRATEGY = "SPY_RSI5_SMA200"
RSI5_VERSION = "1.0.0"
RSI5_ENTRY_THRESHOLD = 35.0
RSI5_EXIT_THRESHOLD = 50.0

CONNORS_STRATEGY = "SPY_CONNORS_PULLBACK"
CONNORS_VERSION = "1.0.0"
CONNORS_ENTRY_THRESHOLD = 10.0
CONNORS_STOP_ATR_MULTIPLE = 3.0

MOM_STRATEGY = "MOM_12_1"
MOM_VERSION = "mom_12_1_shadow_v1"
MOM_UNIVERSE_HASH = "f7b50c2b0c0882df"
NEW_YORK = ZoneInfo("America/New_York")
DAILY_BAR_FINAL_AT = time(16, 15)


@dataclass(frozen=True, slots=True)
class DailyStrategyDecision:
    strategy: str
    version: str
    symbol: str
    session_date: date
    action: str
    close: float
    indicators: dict[str, float | bool | None]
    reason: str

    @property
    def actionable(self) -> bool:
        return self.action in {"enter_next_open", "exit_next_open"}


@dataclass(frozen=True, slots=True)
class MomentumSelection:
    formation_date: date
    intended_entry_date: date
    eligible_count: int
    selected_count: int
    symbols: tuple[str, ...]
    scores: dict[str, float]
    target_weight: float


def _frame(bars: Iterable[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(bars))
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"daily bars are missing {sorted(missing)}")
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )


def completed_daily_bars(
    bars: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Exclude today's Alpaca daily bar until its regular session is final."""
    instant = (now or datetime.now(UTC)).astimezone(NEW_YORK)
    result: list[dict[str, Any]] = []
    for bar in bars:
        stamp = pd.Timestamp(bar["timestamp"])
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        session_date = stamp.tz_convert(NEW_YORK).date()
        if session_date < instant.date() or (
            session_date == instant.date() and instant.time().replace(tzinfo=None) >= DAILY_BAR_FINAL_AT
        ):
            result.append(bar)
    return result


def wilder_rsi(values: pd.Series, period: int) -> pd.Series:
    """The exact EWM/Wilder implementation used by the recovered artifacts."""
    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = average_gain / average_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + relative_strength))
    result = result.mask((average_loss == 0) & (average_gain > 0), 100.0)
    result = result.mask((average_gain == 0) & (average_loss > 0), 0.0)
    return result.mask((average_gain == 0) & (average_loss == 0), 50.0)


def _last_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_spy_rsi5(bars: Iterable[dict[str, Any]], *, is_long: bool) -> DailyStrategyDecision:
    frame = _frame(bars)
    if len(frame) < 200:
        raise ValueError("SPY RSI5/SMA200 requires at least 200 completed daily bars")
    frame["sma200"] = frame["close"].rolling(200).mean()
    frame["rsi5"] = wilder_rsi(frame["close"], 5)
    row = frame.iloc[-1]
    close = float(row["close"])
    sma200 = _last_number(row["sma200"])
    rsi5 = _last_number(row["rsi5"])
    session = row["timestamp"].date()
    indicators = {"sma200": sma200, "rsi5": rsi5, "above_sma200": bool(sma200 and close > sma200)}

    if sma200 is None or rsi5 is None:
        action, reason = "hold", "indicator history is incomplete"
    elif is_long and rsi5 > RSI5_EXIT_THRESHOLD:
        action, reason = "exit_next_open", f"RSI5 {rsi5:.4f} is above {RSI5_EXIT_THRESHOLD:g}"
    elif not is_long and close > sma200 and rsi5 < RSI5_ENTRY_THRESHOLD:
        action, reason = "enter_next_open", (
            f"close {close:.4f} is above SMA200 {sma200:.4f} and RSI5 "
            f"{rsi5:.4f} is below {RSI5_ENTRY_THRESHOLD:g}"
        )
    else:
        action, reason = "hold", "frozen entry/exit conditions are not met"
    return DailyStrategyDecision(
        RSI5_STRATEGY, RSI5_VERSION, "SPY", session, action, close, indicators, reason
    )


def _streak(close: pd.Series) -> pd.Series:
    values = close.to_numpy(dtype=float)
    result = np.zeros(len(values), dtype=float)
    for index in range(1, len(values)):
        if values[index] > values[index - 1]:
            result[index] = result[index - 1] + 1 if result[index - 1] > 0 else 1
        elif values[index] < values[index - 1]:
            result[index] = result[index - 1] - 1 if result[index - 1] < 0 else -1
    return pd.Series(result, index=close.index)


def _prior_percent_rank(one_day_return: pd.Series) -> pd.Series:
    values = one_day_return.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    for index in range(100, len(values)):
        current = values[index]
        prior = values[index - 100 : index]
        if np.isfinite(current) and np.all(np.isfinite(prior)):
            result[index] = np.sum(prior < current)
    return pd.Series(result, index=one_day_return.index)


def evaluate_spy_connors(bars: Iterable[dict[str, Any]], *, is_long: bool) -> DailyStrategyDecision:
    frame = _frame(bars)
    if len(frame) < 200:
        raise ValueError("SPY Connors requires at least 200 completed daily bars")
    close, high, low = frame["close"], frame["high"], frame["low"]
    frame["sma5"] = close.rolling(5).mean()
    frame["sma200"] = close.rolling(200).mean()
    frame["price_rsi3"] = wilder_rsi(close, 3)
    frame["streak_rsi2"] = wilder_rsi(_streak(close), 2)
    frame["pct_rank100"] = _prior_percent_rank(close.pct_change(fill_method=None))
    frame["connors_rsi"] = (
        frame["price_rsi3"] + frame["streak_rsi2"] + frame["pct_rank100"]
    ) / 3.0
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    frame["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    row = frame.iloc[-1]
    last_close = float(row["close"])
    values = {
        name: _last_number(row[name])
        for name in ("sma5", "sma200", "connors_rsi", "price_rsi3", "streak_rsi2", "pct_rank100", "atr14")
    }
    values["above_sma200"] = bool(values["sma200"] and last_close > values["sma200"])
    session = row["timestamp"].date()

    if any(values[name] is None for name in ("sma5", "sma200", "connors_rsi", "atr14")):
        action, reason = "hold", "indicator history is incomplete"
    elif is_long and last_close > float(values["sma5"]):
        action, reason = "exit_next_open", (
            f"close {last_close:.4f} is above SMA5 {float(values['sma5']):.4f}"
        )
    elif (
        not is_long
        and last_close > float(values["sma200"])
        and float(values["connors_rsi"]) < CONNORS_ENTRY_THRESHOLD
    ):
        action, reason = "enter_next_open", (
            f"close is above SMA200 and Connors RSI {float(values['connors_rsi']):.4f} "
            f"is below {CONNORS_ENTRY_THRESHOLD:g}"
        )
    else:
        action, reason = "hold", "frozen entry/exit conditions are not met"
    return DailyStrategyDecision(
        CONNORS_STRATEGY, CONNORS_VERSION, "SPY", session, action, last_close, values, reason
    )


def rank_mom_12_1(
    *,
    formation_date: date,
    intended_entry_date: date,
    close_t: dict[str, float],
    close_lag21: dict[str, float],
    close_lag252: dict[str, float],
    minimum_eligible: int = 2500,
) -> MomentumSelection:
    scores: dict[str, float] = {}
    for symbol in sorted(set(close_t) & set(close_lag21) & set(close_lag252)):
        current = float(close_t[symbol])
        lag21 = float(close_lag21[symbol])
        lag252 = float(close_lag252[symbol])
        if current >= 5 and lag252 > 0:
            scores[symbol.upper()] = lag21 / lag252 - 1.0
    if len(scores) < minimum_eligible:
        raise ValueError(
            f"MOM_12_1 data coverage halt: eligible={len(scores)}, required={minimum_eligible}"
        )
    selected_count = max(1, math.ceil(len(scores) * 0.10))
    ranked = sorted(scores, key=lambda symbol: (scores[symbol], symbol), reverse=True)
    selected = tuple(ranked[:selected_count])
    return MomentumSelection(
        formation_date=formation_date,
        intended_entry_date=intended_entry_date,
        eligible_count=len(scores),
        selected_count=selected_count,
        symbols=selected,
        scores={symbol: scores[symbol] for symbol in selected},
        target_weight=1.0 / selected_count,
    )
