"""Frozen means frozen.

The alpha map's declaration protocol is only worth something if the rows it
measures are the rows the declaration froze. An earlier version read the split
boundaries from the frozen manifest and then loaded signals from
`intraday_trade_flow_features` and outcomes from `candles` -- both live tables
that a nightly ingest rewrites. That combination is worse than no protocol at
all, because it produces a content hash, a declaration id and an immutability
trigger around a measurement that could silently change overnight.

These tests pin the property down from three sides: the loader must not name a
live table at all, its output must be unmoved by arbitrary live-table drift,
and it must still move when the frozen rows themselves differ -- otherwise the
first two would pass on a function that returns a constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.intraday_alpha_map import (
    attach_forward_returns,
    dataset_coverage,
    load_alpha_map_panel,
)

DATASET_ID = 91
SIGNAL_BAR = datetime(2025, 6, 3, 14, 30, tzinfo=UTC)  # 10:30 ET, a 30m bar open
DECISION = datetime(2025, 6, 3, 15, 0, tzinfo=UTC)  # its close, when features are knowable
WINDOW = (datetime(2025, 6, 3, tzinfo=UTC), datetime(2025, 6, 4, tzinfo=UTC))

LIVE_TABLES = (
    "FROM candles",
    "FROM intraday_trade_flow_features",
    "FROM intraday_features",
    "FROM intraday_microstructure_features",
)


def _grid_bar(index: int, *, open_price: float) -> dict:
    return {
        "symbol": "AAA",
        "timeframe": "1m",
        "timestamp": DECISION + timedelta(minutes=index),
        "source": "alpaca_sip",
        "open": open_price,
        "high": open_price + 0.05,
        "low": open_price - 0.05,
        "close": open_price + 0.02,
        "minutes_from_open": None,
        "session_relative_volume": None,
        "distance_from_session_vwap": None,
    }


def _frozen_store(*, grid_slope: float = 0.10, imbalance: float = -0.31) -> dict:
    """One symbol, one 30m signal bar, a 1m outcome grid after it."""
    return {
        "trade_flow": [
            {
                "symbol": "AAA",
                "timeframe": "30m",
                "timestamp": SIGNAL_BAR,
                "signed_trade_imbalance": imbalance,
                "signed_trade_count_imbalance": -0.22,
                "large_trade_share": 0.18,
                "effective_trade_count": 640.0,
                "effective_spread_bps": 1.4,
                "total_volume": 900_000.0,
                "trade_count": 4_100,
                "unclassified_share": 0.03,
            }
        ],
        "candles": [
            {
                "symbol": "AAA",
                "timeframe": "30m",
                "timestamp": SIGNAL_BAR,
                "source": "alpaca_sip",
                "open": 100.0,
                "high": 100.8,
                "low": 99.4,
                "close": 99.6,
                "minutes_from_open": 60,
                "session_relative_volume": 1.35,
                "distance_from_session_vwap": -0.0021,
            },
            *[
                _grid_bar(index, open_price=100.0 + index * grid_slope)
                for index in range(40)
            ],
        ],
    }


class FrozenOnlyConn:
    """Serves the frozen dataset tables and refuses every live one.

    The refusal is the assertion. A loader that reaches for `candles` does not
    get a wrong answer here, it gets an AssertionError naming the table -- which
    is the failure mode this whole test module exists to make impossible to
    reintroduce quietly.
    """

    def __init__(self, store: dict):
        self.store = store
        self.queries: list[str] = []

    def execute(self, query, params=None):
        collapsed = " ".join(query.split())
        self.queries.append(collapsed)
        for table in LIVE_TABLES:
            if table in collapsed:
                raise AssertionError(
                    f"alpha map read a live table ({table}); frozen datasets exist so "
                    "that a measurement cannot change when the ingest runs"
                )
        params = params or ()
        if "FROM research_dataset_trade_flow_features" in collapsed:
            dataset_id, symbols, timeframe, start, end = params
            return _Result(
                [
                    dict(row)
                    for row in self.store["trade_flow"]
                    if dataset_id == DATASET_ID
                    and row["symbol"] in symbols
                    and row["timeframe"] == timeframe
                    and start <= row["timestamp"] < end
                ]
            )
        if "FROM research_dataset_candles" in collapsed:
            dataset_id, symbols, timeframe, start, end = params
            return _Result(
                [
                    dict(row)
                    for row in self.store["candles"]
                    if dataset_id == DATASET_ID
                    and row["symbol"] in symbols
                    and row["timeframe"] == timeframe
                    and start <= row["timestamp"] < end
                ]
            )
        raise AssertionError(f"unexpected query: {collapsed}")


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _panel(conn: FrozenOnlyConn) -> list[dict]:
    panel = load_alpha_map_panel(
        conn,
        dataset_id=DATASET_ID,
        symbols=["AAA"],
        signal_timeframe="30m",
        grid_timeframe="1m",
        start=WINDOW[0],
        end=WINDOW[1],
        cost_model={"conservative_round_trip_bps": 5.0},
    )
    attach_forward_returns(
        panel["observations"], horizons_seconds=(60, 300, 900), grid_seconds=60
    )
    return panel["observations"]


def _signature(observations) -> list[tuple]:
    """A comparable fingerprint of everything the measurement depends on."""
    return [
        (
            row["symbol"],
            row["timestamp"],
            row["session_date"],
            row["slot"],
            row["cost_bps"],
            tuple(sorted((key, value) for key, value in row["features"].items())),
            tuple(
                (horizon, outcome.get("available"), outcome.get("gross_return_bps"))
                for horizon, outcome in sorted(row["forward"].items())
            ),
        )
        for row in observations
    ]


def test_the_panel_never_names_a_live_table():
    conn = FrozenOnlyConn(_frozen_store())

    observations = _panel(conn)

    assert observations, "expected the frozen fixture to produce an observation"
    assert conn.queries, "expected the loader to query something"
    for query in conn.queries:
        assert "research_dataset_" in query


def test_the_measurement_is_unmoved_by_live_table_drift():
    store = _frozen_store()
    conn = FrozenOnlyConn(store)
    before = _signature(_panel(conn))

    # Whatever a backfill, a restatement or a re-ingest does to the live
    # tables, it cannot reach the frozen rows. Simulated here by replacing the
    # live side wholesale -- the connection would raise if it were consulted.
    store["live_candles_rewritten_by_a_nightly_backfill"] = [
        {"symbol": "AAA", "close": 1.0} for _ in range(5_000)
    ]
    store["live_trade_flow_recalculated_under_a_new_version"] = [
        {"symbol": "AAA", "signed_trade_imbalance": 0.99}
    ]
    after = _signature(_panel(FrozenOnlyConn(store)))

    assert before == after


def test_changing_the_frozen_rows_does_change_the_result():
    # Without this the two tests above would pass on a loader that returned a
    # constant, which is not the property being claimed.
    baseline = _signature(_panel(FrozenOnlyConn(_frozen_store())))
    different_signal = _signature(
        _panel(FrozenOnlyConn(_frozen_store(imbalance=0.44)))
    )
    different_outcome = _signature(
        _panel(FrozenOnlyConn(_frozen_store(grid_slope=-0.10)))
    )

    assert baseline != different_signal
    assert baseline != different_outcome


def test_forward_returns_match_a_hand_computed_expectation():
    """The frozen result equals what the fixture says it should be.

    Computed here in plain Python from the same numbers the fake serves, so a
    change in the loader's join, ordering or entry convention shows up as a
    disagreement with arithmetic rather than as a slightly different number
    that still looks plausible.
    """
    observations = _panel(FrozenOnlyConn(_frozen_store(grid_slope=0.10)))
    assert len(observations) == 1
    observation = observations[0]

    # Decision is the 30m bar's close, not its open.
    assert observation["timestamp"] == DECISION
    assert observation["signal_bar_timestamp"] == SIGNAL_BAR
    # 14:30 UTC in June is 10:30 in New York.
    assert observation["slot"] == "10:30"
    assert observation["cost_bps"] == pytest.approx(5.0)

    # Features come from the frozen 30m bar: (99.6 / 100.0) - 1 = -40bps.
    assert observation["features"]["bar_return"] == pytest.approx(-0.004)
    assert observation["features"]["signed_trade_imbalance"] == pytest.approx(-0.31)
    assert observation["features"]["relative_volume"] == pytest.approx(1.35)

    # Entry is the open of the first 1m bar at or after the decision instant:
    # bar 0, open 100.00. The 300s exit is the close of bar 4: 100.40 + 0.02.
    entry = 100.0
    exit_price = 100.0 + 4 * 0.10 + 0.02
    expected_bps = (exit_price / entry - 1) * 10_000

    outcome = observation["forward"][300]
    assert outcome["available"] is True
    assert outcome["entry_price"] == pytest.approx(entry)
    assert outcome["exit_price"] == pytest.approx(exit_price)
    assert outcome["gross_return_bps"] == pytest.approx(expected_bps)
    assert outcome["bars"] == 5


def test_a_dataset_without_a_frozen_outcome_grid_is_refused_by_name():
    store = _frozen_store()
    store["candles"] = [row for row in store["candles"] if row["timeframe"] != "1m"]

    with pytest.raises(ValueError, match="no frozen 1m candles"):
        _panel(FrozenOnlyConn(store))


def test_a_dataset_without_frozen_trade_flow_is_refused_by_name():
    store = _frozen_store()
    store["trade_flow"] = []

    with pytest.raises(ValueError, match="no frozen 30m trade-flow features"):
        _panel(FrozenOnlyConn(store))


def test_dataset_coverage_counts_only_frozen_rows():
    """What `declare` checks before it spends a single-use declaration.

    A declaration can be measured once. If a missing outcome grid only surfaced
    during the run, the declaration would already be spent and fixing the
    dataset would cost a new one.
    """
    store = _frozen_store()
    conn = _CoverageConn(store)

    coverage = dataset_coverage(
        conn,
        dataset_id=DATASET_ID,
        symbols=["AAA"],
        signal_timeframe="30m",
        grid_timeframe="1m",
    )

    assert coverage["trade_flow_rows"] == 1
    assert coverage["grid_candles"] == 40
    assert coverage["grid_symbols"] == 1
    for query in conn.queries:
        assert "research_dataset_" in query


class _CoverageConn(FrozenOnlyConn):
    """Answers the aggregate form of the same two frozen queries."""

    def execute(self, query, params=None):
        collapsed = " ".join(query.split())
        self.queries.append(collapsed)
        for table in LIVE_TABLES:
            if table in collapsed:
                raise AssertionError(f"coverage read a live table ({table})")
        dataset_id, symbols, timeframe = (params or ())[:3]
        source = (
            self.store["trade_flow"]
            if "research_dataset_trade_flow_features" in collapsed
            else self.store["candles"]
        )
        matching = [
            row
            for row in source
            if dataset_id == DATASET_ID
            and row["symbol"] in symbols
            and row["timeframe"] == timeframe
        ]
        return _Result(
            [
                {
                    "rows": len(matching),
                    "symbols": len({row["symbol"] for row in matching}),
                    "first_bar": min((row["timestamp"] for row in matching), default=None),
                    "last_bar": max((row["timestamp"] for row in matching), default=None),
                }
            ]
        )


def test_the_grid_must_be_finer_than_the_signal_timeframe():
    with pytest.raises(ValueError, match="must be finer than the signal"):
        load_alpha_map_panel(
            FrozenOnlyConn(_frozen_store()),
            dataset_id=DATASET_ID,
            symbols=["AAA"],
            signal_timeframe="30m",
            grid_timeframe="30m",
            start=WINDOW[0],
            end=WINDOW[1],
            cost_model={"conservative_round_trip_bps": 5.0},
        )
