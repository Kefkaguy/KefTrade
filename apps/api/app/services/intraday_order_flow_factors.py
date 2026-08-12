"""Factor builders for the order-flow families.

These are the first factors in this system whose score cannot be computed from
OHLCV at any resolution.  The retired gap experiment measured six ways of
rearranging four prices per bar; nothing there could distinguish a million
shares bought from a million shares sold.  Each builder here reads a side
channel the bars do not carry -- premarket price discovery, signed trade
imbalance, or a sector peer group -- and is refused outright when that channel
is absent rather than quietly measured against nothing.

Timing discipline is unchanged and deliberately conservative: the score is
knowable at the close of its signal bar, the decision is taken one bar later,
entry is at the open of the bar after that, and a position is never carried
past the regular close.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services.intraday_sector_flow import sector_relative_bars
from app.services.intraday_session_calendar import ordered_regular_sessions

ORDER_FLOW_FACTOR_VERSION = "intraday_order_flow_factors_v1"

# Predeclared, before any result was seen.
MINIMUM_OPENING_GAP = 0.003
# A gap the premarket barely touched. Below this share, the overnight move was
# not negotiated in extended hours and arrives at the auction unpriced.
MAXIMUM_PREMARKET_DISCOVERY = 0.35
# Premarket that was thin as well as uninformative.
MAXIMUM_PREMARKET_RELATIVE_VOLUME = 1.0

# Signed imbalance strong enough to be one-sided rather than noise.
MINIMUM_TRADE_IMBALANCE = 0.30
# Below this the bar's flow is too sparse for the imbalance to mean anything.
MINIMUM_TRADE_COUNT = 200
# A bar where most prints could not be signed is not evidence about flow.
MAXIMUM_UNCLASSIFIED_SHARE = 0.25

# Idiosyncratic move in peer-dispersion units.
MINIMUM_STANDARDIZED_RESIDUAL = 2.0
# Traded materially harder than its sector at the same instant.
MINIMUM_EXCESS_PARTICIPATION = 2.0


def _observation(
    *,
    factor_key: str,
    symbol: str,
    session_date: date,
    score: float,
    target_return: float,
    signal_bar: dict[str, Any],
    entry_bar: dict[str, Any],
    exit_bar: dict[str, Any],
    timeframe: str,
    horizon_bars: int,
    **extra: Any,
) -> dict[str, Any]:
    from app.services.intraday_factor_diagnostics import _observation as base

    return base(
        factor_key=factor_key,
        symbol=symbol,
        session_date=session_date,
        score=score,
        target_return=target_return,
        signal_bar=signal_bar,
        entry_bar=entry_bar,
        exit_bar=exit_bar,
        timeframe=timeframe,
        horizon_bars=horizon_bars,
        **extra,
    )


def _horizon_return(entry_bar: dict[str, Any], exit_bar: dict[str, Any]) -> float | None:
    entry_price = float(entry_bar["open"])
    if entry_price <= 0:
        return None
    return (float(exit_bar["close"]) - entry_price) / entry_price


# ---------------------------------------------------------------------------
# Family 1: gaps the premarket never priced
# ---------------------------------------------------------------------------


def premarket_undiscovered_gap_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    horizon_bars: int = 1,
    premarket_by_symbol: dict[str, dict[Any, dict[str, Any]]] | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """A gap that arrived at the open unnegotiated should partly revert.

    The economic claim is about *who set the opening price*.  When four and a
    half hours of premarket trading discovered most of a gap, the open reflects
    many participants agreeing.  When the premarket was thin and barely moved,
    the same gap is set by whoever happened to be in the opening auction, and
    the imbalance clears at a price nobody negotiated.

    Every input is known at 09:30.  The decision is taken at 10:00 and entry is
    at the open of the 10:00 bar, so the score never reads its own target.
    """
    if timeframe != "30m" or not premarket_by_symbol:
        return []
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be at least 1")

    decision_index = 1  # the 10:00 bar completes the confirmation
    entry_index = decision_index + 1
    output: list[dict[str, Any]] = []

    for symbol, rows in candles_by_symbol.items():
        sessions = ordered_regular_sessions(rows, timeframe=timeframe)
        premarket = premarket_by_symbol.get(str(symbol).upper()) or {}
        for session_date, session in sessions:
            row = premarket.get(session_date)
            if row is None or len(session) <= entry_index + horizon_bars - 1:
                continue
            gap = row.get("opening_gap")
            discovered = row.get("gap_discovered_premarket")
            relative_volume = row.get("premarket_relative_volume")
            # An unmeasurable gap is not a small one; it is dropped. The
            # premarket builder already leaves these null across a membership
            # hole, where a close-to-open move is not an overnight gap, so the
            # adjacency rule is enforced once at the source rather than
            # re-derived here from whatever candles this run happens to hold.
            if gap is None or discovered is None or relative_volume is None:
                continue
            if abs(float(gap)) < MINIMUM_OPENING_GAP:
                continue
            if abs(float(discovered)) > MAXIMUM_PREMARKET_DISCOVERY:
                continue
            if float(relative_volume) > MAXIMUM_PREMARKET_RELATIVE_VOLUME:
                continue

            entry_bar = session[entry_index]
            exit_bar = session[entry_index + horizon_bars - 1]
            horizon_return = _horizon_return(entry_bar, exit_bar)
            if horizon_return is None:
                continue
            output.append(
                _observation(
                    factor_key="premarket_undiscovered_gap_reversal",
                    symbol=symbol,
                    session_date=session_date,
                    # Reversal: an unpriced gap up is a short, and the score is
                    # signed so a positive score is the profitable direction.
                    score=-float(gap),
                    target_return=horizon_return,
                    signal_bar=session[decision_index],
                    entry_bar=entry_bar,
                    exit_bar=exit_bar,
                    timeframe=timeframe,
                    horizon_bars=horizon_bars,
                    signal_polarity="reversal",
                    opening_gap=float(gap),
                    gap_discovered_premarket=float(discovered),
                    premarket_relative_volume=float(relative_volume),
                    gap_direction="up" if float(gap) > 0 else "down",
                )
            )
    return output


# ---------------------------------------------------------------------------
# Family 2: signed trade imbalance
# ---------------------------------------------------------------------------


def signed_trade_imbalance_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    horizon_bars: int = 1,
    trade_flow_by_symbol: dict[str, dict[datetime, dict[str, Any]]] | None = None,
    trade_imbalance_calibration: dict[str, Any] | None = None,
    signal_polarity: str = "continuation",
    **_: Any,
) -> list[dict[str, Any]]:
    """Persistent one-sided aggression should keep pushing price.

    The forced participant is an institution working a parent order to a
    same-day completion target.  Its child orders cross the spread in one
    direction for as long as the parent is unfilled, which is visible as
    signed imbalance and is invisible in a candle: the bar shows a million
    shares either way.
    """
    if timeframe != "30m" or not trade_flow_by_symbol:
        return []
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be at least 1")
    if signal_polarity not in {"continuation", "reversal"}:
        raise ValueError("signal_polarity must be 'continuation' or 'reversal'")

    output: list[dict[str, Any]] = []
    for symbol, rows in candles_by_symbol.items():
        sessions = ordered_regular_sessions(rows, timeframe=timeframe)
        flow = trade_flow_by_symbol.get(str(symbol).upper()) or {}
        for session_date, session in sessions:
            # The last usable signal bar leaves room for entry plus the hold,
            # so a horizon is never silently truncated at the close.
            last_signal = len(session) - horizon_bars - 1
            for index in range(last_signal):
                signal_bar = session[index]
                row = flow.get(signal_bar["timestamp"])
                if row is None:
                    continue
                imbalance = row.get("signed_trade_imbalance")
                trade_count = row.get("trade_count")
                unclassified = row.get("unclassified_share")
                if imbalance is None or not trade_count:
                    continue
                if int(trade_count) < MINIMUM_TRADE_COUNT:
                    continue
                if (
                    unclassified is not None
                    and float(unclassified) > MAXIMUM_UNCLASSIFIED_SHARE
                ):
                    continue
                if trade_imbalance_calibration is not None and float(
                    row.get("effective_trade_count") or 0
                ) < 50.0:
                    # The calibrated random-sign noise floor is only licensed
                    # for bars with the same minimum effective sample size.
                    continue
                threshold = MINIMUM_TRADE_IMBALANCE
                calibration_id = None
                threshold_mode = "legacy_fixed"
                if trade_imbalance_calibration is not None:
                    report = dict(trade_imbalance_calibration.get("report") or {})
                    calibrated = dict(report.get("threshold") or {})
                    threshold_mode = str(calibrated.get("mode") or "")
                    calibration_id = trade_imbalance_calibration.get("id")
                    if threshold_mode == "global":
                        threshold = float(calibrated["global_rounded_up"])
                    elif threshold_mode == "time_liquidity_bucket":
                        timestamp = signal_bar["timestamp"].astimezone(
                            ZoneInfo("America/New_York")
                        )
                        boundaries = calibrated["liquidity_volume_boundaries"]
                        volume = float(row.get("total_volume") or 0)
                        bucket = (
                            "low" if volume <= float(boundaries[0])
                            else "medium" if volume <= float(boundaries[1])
                            else "high"
                        )
                        bucket_key = f"{timestamp:%H:%M}|{bucket}"
                        try:
                            threshold = float(calibrated["bucket_thresholds"][bucket_key])
                        except KeyError as error:
                            raise ValueError(
                                f"Calibration {calibration_id} has no frozen threshold "
                                f"for discovery bucket {bucket_key}."
                            ) from error
                    else:
                        raise ValueError(
                            f"Calibration {calibration_id} has unsupported threshold mode "
                            f"{threshold_mode!r}."
                        )
                if abs(float(imbalance)) < threshold:
                    continue

                entry_bar = session[index + 1]
                exit_bar = session[index + horizon_bars]
                horizon_return = _horizon_return(entry_bar, exit_bar)
                if horizon_return is None:
                    continue
                output.append(
                    _observation(
                        factor_key="signed_trade_imbalance_continuation",
                        symbol=symbol,
                        session_date=session_date,
                        score=(
                            float(imbalance)
                            if signal_polarity == "continuation"
                            else -float(imbalance)
                        ),
                        target_return=horizon_return,
                        signal_bar=signal_bar,
                        entry_bar=entry_bar,
                        exit_bar=exit_bar,
                        timeframe=timeframe,
                        horizon_bars=horizon_bars,
                        signal_polarity=signal_polarity,
                        signed_trade_imbalance=float(imbalance),
                        trade_count=int(trade_count),
                        unclassified_share=(
                            float(unclassified) if unclassified is not None else None
                        ),
                        minimum_signed_imbalance=float(threshold),
                        threshold_mode=threshold_mode,
                        trade_imbalance_calibration_id=calibration_id,
                    )
                )
    return output


# ---------------------------------------------------------------------------
# Family 3: single-name forced flow against a calm sector
# ---------------------------------------------------------------------------


def sector_relative_forced_flow_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    horizon_bars: int = 1,
    sector_by_symbol: dict[str, str] | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """A name dumped against a flat sector on heavy volume should revert.

    The distinguishing claim is the sector control.  A large move on heavy
    volume that the whole sector shares is repricing, and repricing does not
    revert.  The same move with the sector unmoved is one participant liquidating
    a position for reasons that have nothing to do with the security's value,
    and the concession they pay is what reverts.
    """
    if timeframe != "30m" or not sector_by_symbol:
        return []
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be at least 1")

    sessions_by_symbol = {
        symbol: ordered_regular_sessions(rows, timeframe=timeframe)
        for symbol, rows in candles_by_symbol.items()
    }
    regular_only = {
        symbol: [bar for _session_date, session in sessions for bar in session]
        for symbol, sessions in sessions_by_symbol.items()
    }
    context = sector_relative_bars(regular_only, sector_by_symbol=sector_by_symbol)

    output: list[dict[str, Any]] = []
    for symbol, sessions in sessions_by_symbol.items():
        by_timestamp = context.get(str(symbol).upper()) or {}
        for session_date, session in sessions:
            last_signal = len(session) - horizon_bars - 1
            for index in range(last_signal):
                signal_bar = session[index]
                row = by_timestamp.get(signal_bar["timestamp"])
                if row is None:
                    continue
                residual = row.get("standardized_residual")
                participation = row.get("excess_participation")
                if residual is None or participation is None:
                    continue
                if abs(float(residual)) < MINIMUM_STANDARDIZED_RESIDUAL:
                    continue
                if float(participation) < MINIMUM_EXCESS_PARTICIPATION:
                    continue

                entry_bar = session[index + 1]
                exit_bar = session[index + horizon_bars]
                horizon_return = _horizon_return(entry_bar, exit_bar)
                if horizon_return is None:
                    continue
                output.append(
                    _observation(
                        factor_key="sector_relative_forced_flow_reversal",
                        symbol=symbol,
                        session_date=session_date,
                        # Reversal: the idiosyncratic move is faded, so the
                        # score is the negated residual.
                        score=-float(residual),
                        target_return=horizon_return,
                        signal_bar=signal_bar,
                        entry_bar=entry_bar,
                        exit_bar=exit_bar,
                        timeframe=timeframe,
                        horizon_bars=horizon_bars,
                        signal_polarity="reversal",
                        sector=row.get("sector"),
                        standardized_residual=float(residual),
                        sector_residual_return=row.get("sector_residual_return"),
                        excess_participation=float(participation),
                        peers=row.get("peers"),
                    )
                )
    return output


def horizon_builder(base: Any, *, factor_key: str, horizon_bars: int) -> Any:
    """Bind a predeclared horizon, keeping the factor key distinct per trial."""

    def builder(
        candles_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        timeframe: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return [
            {**row, "factor_key": factor_key}
            for row in base(
                candles_by_symbol,
                timeframe=timeframe,
                horizon_bars=horizon_bars,
                **kwargs,
            )
        ]

    return builder


def calibrated_horizon_builder(
    base: Any, *, factor_key: str, horizon_bars: int, **bound_kwargs: Any
) -> Any:
    """A v2 factor that refuses to run without its immutable calibration."""

    bound = horizon_builder(base, factor_key=factor_key, horizon_bars=horizon_bars)

    def builder(
        candles_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        timeframe: str,
        trade_imbalance_calibration: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if trade_imbalance_calibration is None:
            raise ValueError(f"{factor_key} requires a frozen return-blind calibration.")
        return bound(
            candles_by_symbol,
            timeframe=timeframe,
            trade_imbalance_calibration=trade_imbalance_calibration,
            **bound_kwargs,
            **kwargs,
        )

    return builder
