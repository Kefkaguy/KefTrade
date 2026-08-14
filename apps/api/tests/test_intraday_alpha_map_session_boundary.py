"""Forward returns must stop at the exchange session close.

A frozen 1m outcome grid built from a SIP feed contains after-hours bars, and
they are indistinguishable from regular ones by inspection: same symbol, same
exchange date, perfectly contiguous with the 15:59 bar. Neither the
group-by-session-date nor the gap check excludes them, so a 15:30 ET decision
could "hold" through 16:30 on prices this strategy's universe was never going
to trade at.

The boundary comes from `minutes_to_close` on the frozen signal feature, which
the feature engine computes as `market_close - bar_timestamp` against the XNYS
calendar (app/services/labs/intraday/session.py). That is the only source that
is right on early-close days; a hardcoded 16:00 would silently admit three
extra hours on exactly the sessions nobody checks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.intraday_alpha_map import (
    attach_forward_returns,
    forward_return_ladder,
    load_alpha_map_panel,
    session_close_timestamp,
)

DATASET_ID = 77

# 2025-06-03 is EDT, so the regular session is 13:30-20:00 UTC (09:30-16:00 ET).
REGULAR_DAY = datetime(2025, 6, 3, tzinfo=UTC)
REGULAR_OPEN = datetime(2025, 6, 3, 13, 30, tzinfo=UTC)
REGULAR_CLOSE = datetime(2025, 6, 3, 20, 0, tzinfo=UTC)

# 2025-07-03, the half day before Independence Day: 13:00 ET, i.e. 17:00 UTC.
EARLY_DAY = datetime(2025, 7, 3, tzinfo=UTC)
EARLY_OPEN = datetime(2025, 7, 3, 13, 30, tzinfo=UTC)
EARLY_CLOSE = datetime(2025, 7, 3, 17, 0, tzinfo=UTC)

REGULAR_PRICE = 100.0
# Deliberately far from the regular-session prices: if an after-hours bar ever
# reaches a measurement, the resulting basis points are absurd rather than
# subtly wrong, and the assertion that catches it is unambiguous.
AFTER_HOURS_PRICE = 500.0


def _minute_bars(session_open, session_close, *, minutes_after_close: int) -> list[dict]:
    """A contiguous 1m grid that runs straight through the close.

    No gap at the boundary -- that is the point. The after-hours bars look
    exactly like a continuation of the session.
    """
    rows = []
    cursor = session_open
    while cursor < session_close:
        offset = int((cursor - session_open).total_seconds() // 60)
        price = REGULAR_PRICE + offset * 0.01
        rows.append(_bar(cursor, price))
        cursor += timedelta(minutes=1)
    for index in range(minutes_after_close):
        rows.append(_bar(session_close + timedelta(minutes=index), AFTER_HOURS_PRICE))
    return rows


def _bar(timestamp, price: float) -> dict:
    return {
        "symbol": "AAA",
        "timeframe": "1m",
        "timestamp": timestamp,
        "source": "alpaca_sip",
        "open": price,
        "high": price + 0.02,
        "low": price - 0.02,
        "close": price + 0.01,
        "minutes_from_open": None,
        "minutes_to_close": None,
        "session_relative_volume": None,
        "distance_from_session_vwap": None,
    }


def _signal_bar(timestamp, session_close) -> dict:
    minutes_to_close = int((session_close - timestamp).total_seconds() // 60)
    return {
        "symbol": "AAA",
        "timeframe": "30m",
        "timestamp": timestamp,
        "source": "alpaca_sip",
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.2,
        "minutes_from_open": int((timestamp - REGULAR_OPEN).total_seconds() // 60),
        "minutes_to_close": minutes_to_close,
        "session_relative_volume": 1.1,
        "distance_from_session_vwap": 0.0005,
    }


def _flow(timestamp) -> dict:
    return {
        "symbol": "AAA",
        "timeframe": "30m",
        "timestamp": timestamp,
        "signed_trade_imbalance": -0.25,
        "signed_trade_count_imbalance": -0.20,
        "large_trade_share": 0.15,
        "effective_trade_count": 500.0,
        "effective_spread_bps": 1.2,
        "total_volume": 800_000.0,
        "trade_count": 3_000,
        "unclassified_share": 0.02,
    }


def _store(*, session_open, session_close, signal_opens, minutes_after_close=60) -> dict:
    return {
        "trade_flow": [_flow(item) for item in signal_opens],
        "candles": [
            *[_signal_bar(item, session_close) for item in signal_opens],
            *_minute_bars(session_open, session_close, minutes_after_close=minutes_after_close),
        ],
    }


class FrozenConn:
    def __init__(self, store: dict):
        self.store = store

    def execute(self, query, params=None):
        collapsed = " ".join(query.split())
        params = params or ()
        if "FROM research_dataset_trade_flow_features" in collapsed:
            source = self.store["trade_flow"]
        elif "FROM research_dataset_candles" in collapsed:
            source = self.store["candles"]
        else:
            raise AssertionError(f"unexpected query: {collapsed}")
        _dataset_id, symbols, timeframe, start, end = params
        return _Result(
            [
                dict(row)
                for row in source
                if row["symbol"] in symbols
                and row["timeframe"] == timeframe
                and start <= row["timestamp"] < end
            ]
        )


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _measure(store, *, horizons, day) -> dict:
    panel = load_alpha_map_panel(
        FrozenConn(store),
        dataset_id=DATASET_ID,
        symbols=["AAA"],
        signal_timeframe="30m",
        grid_timeframe="1m",
        start=day,
        end=day + timedelta(days=1),
        cost_model={"conservative_round_trip_bps": 2.0},
    )
    report = attach_forward_returns(
        panel["observations"], horizons_seconds=horizons, grid_seconds=60
    )
    return {"observations": panel["observations"], "report": report}


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def test_session_close_is_derived_from_minutes_to_close_on_the_signal_bar():
    # Matches the feature engine: minutes_to_close = market_close - bar open.
    row = _signal_bar(datetime(2025, 6, 3, 19, 0, tzinfo=UTC), REGULAR_CLOSE)
    assert row["minutes_to_close"] == 60
    assert session_close_timestamp(row) == REGULAR_CLOSE


def test_a_signal_bar_without_minutes_to_close_yields_no_forward_return():
    # Guessing 16:00 would be wrong on exactly the sessions where being wrong
    # is hardest to notice, so the ladder refuses instead.
    row = _signal_bar(datetime(2025, 6, 3, 19, 0, tzinfo=UTC), REGULAR_CLOSE)
    row["minutes_to_close"] = None
    assert session_close_timestamp(row) is None

    ladder = forward_return_ladder(
        _minute_bars(REGULAR_OPEN, REGULAR_CLOSE, minutes_after_close=0),
        decision_timestamp=datetime(2025, 6, 3, 19, 30, tzinfo=UTC),
        horizons_seconds=(60, 1_800),
        grid_seconds=60,
        session_close_timestamp=None,
    )
    assert {outcome["reason"] for outcome in ladder.values()} == {"session_close_unknown"}


# ---------------------------------------------------------------------------
# Normal full session
# ---------------------------------------------------------------------------


def test_a_midsession_decision_measures_every_horizon_normally():
    store = _store(
        session_open=REGULAR_OPEN,
        session_close=REGULAR_CLOSE,
        signal_opens=[datetime(2025, 6, 3, 15, 0, tzinfo=UTC)],  # 11:00 ET
    )
    result = _measure(store, horizons=(60, 300, 1_800, 3_600), day=REGULAR_DAY)

    forward = result["observations"][0]["forward"]
    assert [forward[h]["available"] for h in (60, 300, 1_800, 3_600)] == [True] * 4
    # Prices climb 0.01/min from the open, so every rung is positive and the
    # longer the horizon the larger the move.
    assert forward[3_600]["gross_return_bps"] > forward[60]["gross_return_bps"] > 0
    assert result["report"]["available_by_horizon"] == {
        "60s": 1,
        "300s": 1,
        "1800s": 1,
        "3600s": 1,
    }


# ---------------------------------------------------------------------------
# The last signal bar of the session
# ---------------------------------------------------------------------------


def test_the_last_30m_signal_cannot_use_after_hours_bars():
    # The 15:30 ET bar decides at 16:00 ET, which is the close. There is no
    # time left to hold anything, and the after-hours bars sitting right there
    # do not change that.
    store = _store(
        session_open=REGULAR_OPEN,
        session_close=REGULAR_CLOSE,
        signal_opens=[datetime(2025, 6, 3, 19, 30, tzinfo=UTC)],  # 15:30 ET
    )
    result = _measure(store, horizons=(60, 300, 1_800), day=REGULAR_DAY)

    observation = result["observations"][0]
    assert observation["timestamp"] == REGULAR_CLOSE
    assert observation["session_close"] == REGULAR_CLOSE
    assert all(not outcome["available"] for outcome in observation["forward"].values())
    assert {outcome["reason"] for outcome in observation["forward"].values()} == {
        "decision_at_or_after_session_close"
    }
    assert result["report"]["available_by_horizon"] == {}


# ---------------------------------------------------------------------------
# A horizon that would cross the close
# ---------------------------------------------------------------------------


def test_a_60m_horizon_from_1530_et_cannot_run_to_1630():
    # The 15:00 ET bar decides at 15:30 ET with 30 minutes of session left.
    # A 30m hold exits exactly at the close and is fine; a 60m hold would exit
    # at 16:30 and is refused.
    store = _store(
        session_open=REGULAR_OPEN,
        session_close=REGULAR_CLOSE,
        signal_opens=[datetime(2025, 6, 3, 19, 0, tzinfo=UTC)],  # 15:00 ET
    )
    result = _measure(store, horizons=(1_800, 3_600), day=REGULAR_DAY)

    forward = result["observations"][0]["forward"]
    assert result["observations"][0]["timestamp"] == datetime(2025, 6, 3, 19, 30, tzinfo=UTC)
    assert forward[1_800]["available"] is True
    assert forward[3_600] == {"available": False, "reason": "session_end_before_horizon"}


def test_the_horizon_that_exits_exactly_at_the_close_is_kept():
    # Off-by-one guard in the tightening direction: refusing a hold that ends
    # precisely at the close would throw away the last legitimate rung of every
    # session, which is a bias toward mid-session observations.
    store = _store(
        session_open=REGULAR_OPEN,
        session_close=REGULAR_CLOSE,
        signal_opens=[datetime(2025, 6, 3, 19, 0, tzinfo=UTC)],
    )
    result = _measure(store, horizons=(1_740, 1_800, 1_860), day=REGULAR_DAY)

    forward = result["observations"][0]["forward"]
    assert forward[1_740]["available"] is True  # exits 15:59
    assert forward[1_800]["available"] is True  # exits exactly 16:00
    assert forward[1_860]["reason"] == "session_end_before_horizon"  # 16:01


# ---------------------------------------------------------------------------
# Early close
# ---------------------------------------------------------------------------


def test_an_early_close_session_cannot_use_bars_from_1300_et_onward():
    # The trap a hardcoded 16:00 would fall into: on 2025-07-03 the exchange
    # closes at 13:00 ET, and there are three further hours of contiguous bars
    # that a clock-based filter would happily consume.
    store = _store(
        session_open=EARLY_OPEN,
        session_close=EARLY_CLOSE,
        signal_opens=[datetime(2025, 7, 3, 16, 0, tzinfo=UTC)],  # 12:00 ET
        minutes_after_close=180,
    )
    result = _measure(store, horizons=(1_800, 3_600), day=EARLY_DAY)

    observation = result["observations"][0]
    assert observation["session_close"] == EARLY_CLOSE
    assert observation["timestamp"] == datetime(2025, 7, 3, 16, 30, tzinfo=UTC)  # 12:30 ET
    # 30m exits at 13:00, the early close, and is kept. 60m would exit at 13:30.
    assert observation["forward"][1_800]["available"] is True
    assert observation["forward"][3_600]["reason"] == "session_end_before_horizon"


def test_an_early_close_does_not_shorten_the_following_regular_session():
    # The boundary is per observation, read from that bar's own feature, so a
    # half day cannot leak into a normal one.
    regular = _store(
        session_open=REGULAR_OPEN,
        session_close=REGULAR_CLOSE,
        signal_opens=[datetime(2025, 6, 3, 16, 0, tzinfo=UTC)],  # 12:00 ET
    )
    result = _measure(regular, horizons=(3_600,), day=REGULAR_DAY)

    assert result["observations"][0]["session_close"] == REGULAR_CLOSE
    assert result["observations"][0]["forward"][3_600]["available"] is True


# ---------------------------------------------------------------------------
# After-hours bars are present and ignored
# ---------------------------------------------------------------------------


def test_contiguous_after_hours_candles_are_present_and_never_priced():
    store = _store(
        session_open=REGULAR_OPEN,
        session_close=REGULAR_CLOSE,
        signal_opens=[datetime(2025, 6, 3, 19, 0, tzinfo=UTC)],  # 15:00 ET
        minutes_after_close=60,
    )
    # The trap is genuinely laid: the grid holds after-hours bars on the same
    # exchange date, contiguous with the 15:59 bar, at a wildly different price.
    after_hours = [row for row in store["candles"] if row["timestamp"] >= REGULAR_CLOSE]
    assert len(after_hours) == 60
    assert after_hours[0]["timestamp"] == REGULAR_CLOSE
    assert after_hours[0]["open"] == AFTER_HOURS_PRICE

    result = _measure(store, horizons=(1_800, 3_600), day=REGULAR_DAY)
    forward = result["observations"][0]["forward"]

    # The 30m rung exits on the last regular bar (15:59), not on an after-hours
    # one. Its price is a normal continuation of the session, ~101.7 rather
    # than 500.
    last_regular_close = REGULAR_PRICE + 389 * 0.01 + 0.01
    assert forward[1_800]["exit_price"] == pytest.approx(last_regular_close)
    assert forward[1_800]["gross_return_bps"] < 200
    # And the rung that would have needed those bars is refused rather than
    # silently earning the 400% jump.
    assert forward[3_600]["reason"] == "session_end_before_horizon"


def test_removing_the_after_hours_bars_changes_nothing():
    """The strongest form: the measurement is identical with and without them.

    If any rung were quietly consuming after-hours prices, deleting those bars
    would move a number. Nothing moves.
    """
    horizons = (60, 300, 1_800, 3_600)
    day = REGULAR_DAY
    signal_opens = [
        datetime(2025, 6, 3, 15, 0, tzinfo=UTC),
        datetime(2025, 6, 3, 19, 0, tzinfo=UTC),
        datetime(2025, 6, 3, 19, 30, tzinfo=UTC),
    ]
    with_after_hours = _measure(
        _store(
            session_open=REGULAR_OPEN,
            session_close=REGULAR_CLOSE,
            signal_opens=signal_opens,
            minutes_after_close=120,
        ),
        horizons=horizons,
        day=day,
    )
    without = _measure(
        _store(
            session_open=REGULAR_OPEN,
            session_close=REGULAR_CLOSE,
            signal_opens=signal_opens,
            minutes_after_close=0,
        ),
        horizons=horizons,
        day=day,
    )

    def signature(result):
        return [
            (
                row["timestamp"],
                row["session_close"],
                tuple(
                    (horizon, outcome.get("available"), outcome.get("gross_return_bps"))
                    for horizon, outcome in sorted(row["forward"].items())
                ),
            )
            for row in result["observations"]
        ]

    assert signature(with_after_hours) == signature(without)
    assert with_after_hours["report"] == without["report"]
    # Not vacuous: the fixture really did produce measurable rungs.
    assert with_after_hours["report"]["available_by_horizon"]["60s"] == 2
