from decimal import Decimal

from app.services.labs.intraday.features import compute_intraday_features


def make_session_candles(day: str, base: float, *, bars: int = 4, volumes: list[float] | None = None) -> list[dict]:
    minutes = ["14:30", "14:45", "15:00", "15:15", "15:30", "15:45"][:bars]
    volumes = volumes or [1000.0 + 100 * index for index in range(bars)]
    rows = []
    for index, minute in enumerate(minutes):
        price = base + index * 0.5
        rows.append(
            {
                "symbol": "TEST",
                "timeframe": "15m",
                "timestamp": f"{day} {minute}:00+00:00",
                "open": price,
                "high": price + 0.3,
                "low": price - 0.3,
                "close": price + 0.1,
                "volume": volumes[index],
            }
        )
    return rows


def make_multi_session_candles(days: list[str], *, bars: int = 4) -> list[dict]:
    candles = []
    for index, day in enumerate(days):
        candles.extend(make_session_candles(day, base=100.0 + index * 2.0, bars=bars))
    return candles


# --- VWAP resets every session ------------------------------------------------

def test_vwap_resets_at_the_start_of_every_session() -> None:
    candles = make_multi_session_candles(["2026-11-23", "2026-11-24"])
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    day1_first = rows[0]
    day2_first = rows[4]
    # The first bar of each session must have VWAP equal to that bar's own
    # typical price -- if VWAP carried over from the prior session, day 2's
    # first-bar VWAP would be pulled toward day 1's (much lower) prices.
    typical_price_day2_bar0 = (candles[4]["high"] + candles[4]["low"] + candles[4]["close"]) / 3.0
    assert float(day2_first["session_vwap"]) == round(typical_price_day2_bar0, 12)
    assert day2_first["session_vwap"] != day1_first["session_vwap"]
    assert float(day2_first["session_vwap"]) > 101.0  # in day 2's price range, not day 1's


def test_vwap_is_cumulative_within_a_session() -> None:
    candles = make_session_candles("2026-11-23", base=100.0, bars=4)
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    vwaps = [float(row["session_vwap"]) for row in rows]
    # Prices rise through the session and VWAP is a cumulative average of
    # rising typical prices, so it must be monotonically non-decreasing.
    assert all(later >= earlier for earlier, later in zip(vwaps, vwaps[1:]))


# --- Opening range uses only the first configured minutes ---------------------

def test_opening_range_freezes_after_the_configured_window() -> None:
    candles = make_session_candles("2026-11-23", base=100.0, bars=4)  # bars at mfo 0,15,30,45
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    # Window is [0, 30): only bars at minutes_from_open 0 and 15 are inside it.
    assert rows[0]["minutes_from_open"] == 0
    assert rows[1]["minutes_from_open"] == 15
    assert rows[2]["minutes_from_open"] == 30
    assert rows[3]["minutes_from_open"] == 45

    # Expanding while inside the window:
    assert rows[0]["opening_range_high"] == Decimal(str(round(candles[0]["high"], 12)))
    assert rows[1]["opening_range_high"] == Decimal(str(round(candles[1]["high"], 12)))  # bar 1 is the higher high

    # Frozen at the window's final value for every bar afterward.
    assert rows[2]["opening_range_high"] == rows[1]["opening_range_high"]
    assert rows[3]["opening_range_high"] == rows[1]["opening_range_high"]
    assert rows[2]["opening_range_low"] == rows[1]["opening_range_low"]
    assert rows[3]["opening_range_low"] == rows[1]["opening_range_low"]


def test_opening_range_window_length_is_configurable() -> None:
    candles = make_session_candles("2026-11-23", base=100.0, bars=4)
    narrow = compute_intraday_features(candles, opening_range_minutes=1, relative_volume_lookback_sessions=20)
    wide = compute_intraday_features(candles, opening_range_minutes=60, relative_volume_lookback_sessions=20)
    # A 1-minute window only ever includes the very first bar; a 60-minute
    # window includes all four bars (mfo 0/15/30/45 all < 60).
    assert narrow[-1]["opening_range_high"] == Decimal(str(round(candles[0]["high"], 12)))
    assert wide[-1]["opening_range_high"] == Decimal(str(round(max(c["high"] for c in candles), 12)))


# --- No look-ahead leakage: truncating the future must not change the past ----

def test_truncating_future_bars_does_not_change_earlier_computed_values() -> None:
    """The definitive no-look-ahead property test: recomputing on a prefix of
    the same candle history must reproduce byte-identical values for every
    bar in that prefix, for every field. If any computation secretly used a
    later bar, truncating that later bar would change the earlier result."""
    candles = make_multi_session_candles(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-23"], bars=4)
    full = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    for cutoff in (1, 4, 5, 9, 13, 17):
        truncated = compute_intraday_features(candles[:cutoff], opening_range_minutes=30, relative_volume_lookback_sessions=20)
        assert len(truncated) == cutoff
        for index in range(cutoff):
            assert truncated[index] == full[index], f"mismatch at cutoff={cutoff} index={index}"


# --- Gap uses the previous VALID session close --------------------------------

def test_gap_percent_uses_previous_session_close_skipping_weekend() -> None:
    # Friday 2026-11-20, then Monday 2026-11-23 (skips the weekend).
    candles = make_multi_session_candles(["2026-11-20", "2026-11-23"], bars=2)
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    friday_close = candles[1]["close"]  # last bar of the first session
    monday_open = candles[2]["open"]  # first bar of the second session
    expected_gap = (monday_open - friday_close) / friday_close
    assert rows[0]["gap_percent"] is None  # no prior session in the data at all
    assert float(rows[2]["gap_percent"]) == round(expected_gap, 12)
    assert float(rows[3]["gap_percent"]) == round(expected_gap, 12)  # constant across the session


def test_gap_percent_is_null_when_the_previous_session_has_no_data() -> None:
    """A session with a real calendar predecessor that simply has no candles
    in our data (a data gap) must yield a null gap, never a value silently
    computed against stale or wrong data."""
    # 2026-11-20 (Friday) and 2026-11-24 (the following Tuesday) are both
    # valid trading days, but 2026-11-23 (Monday) between them is missing
    # from our candle set entirely.
    candles = make_multi_session_candles(["2026-11-20", "2026-11-24"], bars=2)
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    assert rows[2]["gap_percent"] is None
    assert rows[3]["gap_percent"] is None


# --- Session-relative volume computed without future bars ---------------------

def test_relative_volume_is_null_before_the_minimum_prior_sessions_exist() -> None:
    candles = make_multi_session_candles(["2026-11-16", "2026-11-17"], bars=4)  # only 1 prior session available
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    assert all(row["session_relative_volume"] is None for row in rows)


def test_relative_volume_uses_only_prior_sessions_not_future_ones() -> None:
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-23"]
    candles = make_multi_session_candles(days, bars=4)
    full = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    # Recompute with the LAST session's volumes changed drastically; every
    # bar belonging to EARLIER sessions must be completely unaffected.
    inflated = [dict(row) for row in candles]
    for row in inflated[-4:]:
        row["volume"] = row["volume"] * 1000
    recomputed = compute_intraday_features(inflated, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    for index in range(16):  # first 4 sessions (16 bars) must be untouched
        assert recomputed[index]["session_relative_volume"] == full[index]["session_relative_volume"]


# --- Early-close sessions are handled -----------------------------------------

def test_early_close_session_bars_have_correct_minutes_to_close() -> None:
    # 2026-11-27 is the day after Thanksgiving: a real NYSE early close at 13:00 ET (18:00 UTC).
    candles = [
        {"symbol": "TEST", "timeframe": "15m", "timestamp": "2026-11-27 14:30:00+00:00", "open": 100.0, "high": 100.3, "low": 99.7, "close": 100.1, "volume": 1000.0},
        {"symbol": "TEST", "timeframe": "15m", "timestamp": "2026-11-27 17:45:00+00:00", "open": 101.0, "high": 101.3, "low": 100.7, "close": 101.1, "volume": 1100.0},
    ]
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    assert rows[0]["minutes_to_close"] == 210  # 14:30 -> 18:00 close = 3h30m
    assert rows[1]["minutes_to_close"] == 15  # 17:45 -> 18:00 close


def test_bars_at_or_after_the_early_close_boundary_are_excluded() -> None:
    candles = [
        {"symbol": "TEST", "timeframe": "15m", "timestamp": "2026-11-27 14:30:00+00:00", "open": 100.0, "high": 100.3, "low": 99.7, "close": 100.1, "volume": 1000.0},
        {"symbol": "TEST", "timeframe": "15m", "timestamp": "2026-11-27 18:00:00+00:00", "open": 102.0, "high": 102.3, "low": 101.7, "close": 102.1, "volume": 1200.0},  # at the close boundary
    ]
    rows = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    assert len(rows) == 1
    assert rows[0]["timestamp"].isoformat().startswith("2026-11-27T14:30")


# --- Idempotency / backfill vs incremental -------------------------------------

def test_computation_is_idempotent() -> None:
    candles = make_multi_session_candles(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"], bars=4)
    first = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    second = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)
    assert first == second


def test_backfill_and_incremental_computation_produce_identical_results() -> None:
    """`sync_intraday_features` always recomputes from the full available
    candle history rather than a delta (see its docstring for why); this
    proves that design choice: computing on the whole history at once
    ("backfill") and computing on a growing prefix repeatedly
    ("incremental", simulated by calling compute() again each time more
    candles are available) produce identical values for every bar common to
    both calls."""
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-23"]
    candles = make_multi_session_candles(days, bars=4)
    backfill_result = compute_intraday_features(candles, opening_range_minutes=30, relative_volume_lookback_sessions=20)

    # Simulate incremental sync: compute after each new session's candles
    # arrive, and check the newly-visible bars match backfill exactly.
    for session_index in range(1, len(days) + 1):
        prefix = candles[: session_index * 4]
        incremental_result = compute_intraday_features(prefix, opening_range_minutes=30, relative_volume_lookback_sessions=20)
        assert incremental_result == backfill_result[: session_index * 4]


# --- Configuration is respected, not hard-coded --------------------------------

def test_defaults_come_from_settings_not_a_hardcoded_literal(monkeypatch) -> None:
    from app.services.labs.intraday import features as features_module

    monkeypatch.setattr(features_module.settings, "intraday_opening_range_minutes", 45, raising=False)
    assert features_module.default_opening_range_minutes() == 45


def test_empty_candle_list_returns_empty_features() -> None:
    assert compute_intraday_features([]) == []
