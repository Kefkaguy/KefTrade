"""The outcome grid must never be resident all at once.

The alpha map's signal side is one bar per 15 or 30 minutes and fits in memory
without thinking about it. Its outcome side does not: a discovery window over a
few dozen symbols is millions of 1m bars, and loading them the obvious way --
`fetchall()` into a list of dicts -- holds libpq's whole result buffer and one
Python dict per row at the same instant. On the production VPS that was 7.9GiB
resident and an OOM kill (exit 137) before a single cell had been measured.

The fix has two halves and this module pins both:

* candles stream through a server-side cursor, so no result is materialized
  twice, and
* the panel never holds more than one symbol's grid, because panel construction
  needs only to know *which sessions* the grid covers, and the bars themselves
  are read one symbol at a time when the forward ladder is attached.

The second half is the one that matters, and it is the one that is easy to
regress by accident: any change that puts a bar list back on the observation
re-creates the original failure, because every observation would again pin its
session's bars until the whole panel had been measured.

What must not change is the measurement. The equivalence test below computes
the same ladders from a plainly-built, fully-resident grid and requires the
streamed panel to agree exactly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.intraday_alpha_map import (
    DATASET_CANDLE_ITERSIZE,
    _dataset_candles_by_symbol,
    _FrozenOutcomeGrid,
    _GridBar,
    attach_forward_returns,
    forward_return_ladder,
    load_alpha_map_panel,
)
from app.services.intraday_research_integrity import exchange_session_date

DATASET_ID = 404
SYMBOLS = ("AAA", "BBB", "CCC")
SESSIONS = (date(2025, 6, 3), date(2025, 6, 4))
HORIZONS = (60, 300, 1_800)
COST_MODEL = {"conservative_round_trip_bps": 3.0}

# June is EDT: the regular session is 13:30-20:00 UTC (09:30-16:00 ET).
SESSION_MINUTES = 390
SIGNAL_OPENS_ET_OFFSET = (90, 210)  # 11:00 and 13:00 ET, both mid-session


def _session_open(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 13, 30, tzinfo=UTC)


def _session_close(session: date) -> datetime:
    return _session_open(session) + timedelta(minutes=SESSION_MINUTES)


def _price(symbol: str, session: date, minute: int) -> float:
    """Distinct per symbol and session, so a mixed-up grid cannot pass."""
    base = 100.0 + 10.0 * SYMBOLS.index(symbol) + SESSIONS.index(session)
    return base + minute * 0.01


def _grid_bar(symbol: str, session: date, minute: int) -> dict:
    price = _price(symbol, session, minute)
    return {
        "symbol": symbol,
        "timeframe": "1m",
        "timestamp": _session_open(session) + timedelta(minutes=minute),
        "source": "alpaca_sip",
        "open": price,
        "high": price + 0.02,
        "low": price - 0.02,
        "close": price + 0.01,
        "volume": 1_000.0,
        # The 1m grid has no session context in the frozen dataset: the
        # intraday feature table is CHECK-constrained to 15m/30m. The fake
        # connection strips these whenever the query has no join, so a loader
        # that reintroduced the join would be visible here.
        "minutes_from_open": None,
        "minutes_to_close": None,
        "session_relative_volume": None,
        "distance_from_session_vwap": None,
    }


def _signal_bar(symbol: str, session: date, minute: int) -> dict:
    timestamp = _session_open(session) + timedelta(minutes=minute)
    return {
        "symbol": symbol,
        "timeframe": "30m",
        "timestamp": timestamp,
        "source": "alpaca_sip",
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.2,
        "volume": 500_000.0,
        "minutes_from_open": minute,
        "minutes_to_close": SESSION_MINUTES - minute,
        "session_relative_volume": 1.1,
        "distance_from_session_vwap": 0.0004,
    }


def _flow(symbol: str, session: date, minute: int) -> dict:
    return {
        "symbol": symbol,
        "timeframe": "30m",
        "timestamp": _session_open(session) + timedelta(minutes=minute),
        "signed_trade_imbalance": -0.25,
        "signed_trade_count_imbalance": -0.20,
        "large_trade_share": 0.15,
        "effective_trade_count": 500.0,
        "effective_spread_bps": 1.2,
        "total_volume": 800_000.0,
        "trade_count": 3_000,
        "unclassified_share": 0.02,
    }


def _store(*, symbols=SYMBOLS, sessions=SESSIONS) -> dict:
    candles: list[dict] = []
    trade_flow: list[dict] = []
    for symbol in symbols:
        for session in sessions:
            for minute in range(SESSION_MINUTES):
                candles.append(_grid_bar(symbol, session, minute))
            for minute in SIGNAL_OPENS_ET_OFFSET:
                candles.append(_signal_bar(symbol, session, minute))
                trade_flow.append(_flow(symbol, session, minute))
    return {"candles": candles, "trade_flow": trade_flow}


# ---------------------------------------------------------------------------
# A connection that only answers through the streaming protocol
# ---------------------------------------------------------------------------

SESSION_CONTEXT_FIELDS = (
    "minutes_from_open",
    "minutes_to_close",
    "session_relative_volume",
    "distance_from_session_vwap",
)


class StreamingConn:
    """Serves frozen rows, and only ever hands them out one at a time.

    `fetchall` on a candle query raises. That is the assertion: it is exactly
    the call that killed the process, and a loader that reintroduces it fails
    here instead of on the VPS at 3am.
    """

    def __init__(self, store: dict):
        self.store = store
        self.queries: list[str] = []
        self.grid_loads: list[list[str]] = []
        self.presence_loads: list[list[str]] = []
        self.itersizes: list[int] = []
        # Set by `watch_residency` to assert that the previous symbol's bars are
        # already gone by the time the next symbol's stream opens.
        self._watched: _FrozenOutcomeGrid | None = None
        self.residency: list[int] = []

    def watch_residency(self, grid: _FrozenOutcomeGrid) -> None:
        self._watched = grid

    # -- row service --------------------------------------------------------

    def _rows_for(self, query, params):
        collapsed = " ".join(query.split())
        self.queries.append(collapsed)
        params = params or ()
        dataset_id, symbols, timeframe, start, end = params
        assert dataset_id == DATASET_ID
        if "FROM research_dataset_trade_flow_features" in collapsed:
            source = self.store["trade_flow"]
        elif "FROM research_dataset_candles" in collapsed:
            source = self.store["candles"]
            self._record_candle_query(collapsed, list(symbols))
        else:
            raise AssertionError(f"unexpected query: {collapsed}")
        joined = "LEFT JOIN research_dataset_intraday_features" in collapsed
        rows = [
            dict(row)
            for row in source
            if row["symbol"] in symbols
            and row["timeframe"] == timeframe
            and start <= row["timestamp"] < end
        ]
        if "FROM research_dataset_candles" in collapsed and not joined:
            # The unjoined query does not select the session-context columns, so
            # neither does this. A loader that expected them from the grid would
            # see a KeyError rather than a silent None.
            for row in rows:
                for field in SESSION_CONTEXT_FIELDS:
                    row.pop(field, None)
        return rows

    def _record_candle_query(self, collapsed: str, symbols: list[str]) -> None:
        if "c.open" not in collapsed:
            self.presence_loads.append(symbols)
            return
        if "LEFT JOIN research_dataset_intraday_features" in collapsed:
            # The signal side, which is small and still loaded whole.
            return
        if self._watched is not None:
            self.residency.append(
                sum(len(bars) for bars in self._watched._sessions.values())
            )
        self.grid_loads.append(symbols)

    # -- psycopg surface ----------------------------------------------------

    def execute(self, query, params=None):
        return _Result(self._rows_for(query, params))

    def cursor(self, name=None, withhold=False, **kwargs):
        assert name, "candles must stream through a named (server-side) cursor"
        return _ServerCursor(self)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _ServerCursor:
    def __init__(self, conn: StreamingConn):
        self.conn = conn
        self.itersize = None
        self._rows: list[dict] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def execute(self, query, params=None):
        self._rows = self.conn._rows_for(query, params)
        return self

    def __iter__(self):
        assert isinstance(self.itersize, int) and self.itersize > 0, (
            "a named cursor with no itersize fetches the whole result in one "
            "block, which is the thing this loader exists to avoid"
        )
        self.conn.itersizes.append(self.itersize)
        return iter(self._rows)

    def fetchall(self):
        raise AssertionError(
            "the frozen candle stream must be iterated, not materialized: "
            "fetchall() on the 1m outcome grid is what OOM-killed the run"
        )


def _panel(conn: StreamingConn, *, symbols=SYMBOLS) -> dict:
    return load_alpha_map_panel(
        conn,
        dataset_id=DATASET_ID,
        symbols=list(symbols),
        signal_timeframe="30m",
        grid_timeframe="1m",
        start=datetime(2025, 6, 3, tzinfo=UTC),
        end=datetime(2025, 6, 5, tzinfo=UTC),
        cost_model=COST_MODEL,
    )


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_candles_are_never_drained_with_fetchall():
    """The whole point, stated as bluntly as the fake can state it."""
    conn = StreamingConn(_store())

    panel = _panel(conn)
    attach_forward_returns(
        panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )

    # 2340 grid bars and 12 signal bars served, none of them through fetchall --
    # `_ServerCursor.fetchall` raises, so reaching this line is the assertion.
    assert panel["observations"], "expected the fixture to produce observations"
    assert conn.itersizes, "expected the loader to iterate a named cursor"
    assert set(conn.itersizes) == {DATASET_CANDLE_ITERSIZE}
    assert DATASET_CANDLE_ITERSIZE > 0


def test_signal_candles_still_carry_frozen_session_context():
    """minutes_to_close is the session boundary; the join must survive."""
    conn = StreamingConn(_store())

    loaded = _dataset_candles_by_symbol(
        conn,
        dataset_id=DATASET_ID,
        symbols=["AAA"],
        timeframe="30m",
        start=datetime(2025, 6, 3, tzinfo=UTC),
        end=datetime(2025, 6, 5, tzinfo=UTC),
        include_session_context=True,
    )

    assert set(loaded) == {"AAA"}
    assert len(loaded["AAA"]) == len(SESSIONS) * len(SIGNAL_OPENS_ET_OFFSET)
    for row in loaded["AAA"]:
        for field in SESSION_CONTEXT_FIELDS:
            assert field in row
        assert row["minutes_to_close"] is not None
    assert any(
        "LEFT JOIN research_dataset_intraday_features" in query
        for query in conn.queries
    )


def test_outcome_grid_candles_load_without_session_context():
    """And without the join whose cost started this whole thread."""
    conn = StreamingConn(_store())

    loaded = _dataset_candles_by_symbol(
        conn,
        dataset_id=DATASET_ID,
        symbols=["AAA"],
        timeframe="1m",
        start=datetime(2025, 6, 3, tzinfo=UTC),
        end=datetime(2025, 6, 5, tzinfo=UTC),
        include_session_context=False,
    )

    assert len(loaded["AAA"]) == len(SESSIONS) * SESSION_MINUTES
    for row in loaded["AAA"]:
        assert row["open"] is not None
        for field in SESSION_CONTEXT_FIELDS:
            assert field not in row
    assert conn.queries
    for query in conn.queries:
        assert "research_dataset_intraday_features" not in query
        assert "research_dataset_" in query


# ---------------------------------------------------------------------------
# Bounded residency
# ---------------------------------------------------------------------------


def test_panel_construction_reads_no_outcome_bars_at_all():
    """Building the panel needs session coverage, not prices.

    This is what makes one-symbol residency possible: if the panel had to look
    at the bars to decide which observations to keep, it would have to hold
    them, and it would hold all of them at once.
    """
    conn = StreamingConn(_store())

    panel = _panel(conn)

    assert panel["observations"]
    assert conn.grid_loads == [], "the panel loaded outcome bars before it needed them"
    assert conn.presence_loads == [list(SYMBOLS)]
    presence = [query for query in conn.queries if "c.open" not in query and "c.symbol" in query]
    assert presence, "expected a two-column presence pass over the grid"
    assert "LEFT JOIN" not in presence[0]


def test_the_grid_is_loaded_one_symbol_at_a_time_and_released_between():
    """The memory bound, asserted on the mechanism rather than on a number.

    `residency` records how many bars the grid still held at the instant each
    symbol's stream opened. All zeros means the previous symbol was dropped
    before the next was allocated, so the peak is one symbol's bars and not the
    whole discovery window's.
    """
    conn = StreamingConn(_store())
    panel = _panel(conn)
    grid = panel["observations"][0]["_grid"]
    conn.watch_residency(grid)

    attach_forward_returns(
        panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )

    assert conn.grid_loads == [["AAA"], ["BBB"], ["CCC"]]
    assert conn.residency == [0, 0, 0]
    # And having finished, only the last symbol is still held -- one symbol's
    # sessions, not three.
    assert set(grid._sessions) == set(SESSIONS)
    assert sum(len(bars) for bars in grid._sessions.values()) == (
        len(SESSIONS) * SESSION_MINUTES
    )


def test_every_symbol_is_loaded_exactly_once():
    """A cache of one is only a bound if the walk does not thrash it.

    Observations arrive grouped by symbol and `attach_forward_returns` keeps
    them that way; if it stopped doing so, this would still be correct but
    would re-query the grid once per observation.
    """
    conn = StreamingConn(_store())
    panel = _panel(conn)

    attach_forward_returns(
        panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )

    assert [symbols[0] for symbols in conn.grid_loads] == list(SYMBOLS)
    assert len(conn.grid_loads) == len(SYMBOLS)
    for symbols in conn.grid_loads:
        assert len(symbols) == 1, "a per-symbol load must not fan out to the universe"


def test_grid_bars_keep_only_what_the_ladder_reads():
    """The other half of the memory fix, and a guard on its blast radius."""
    bar = _GridBar(
        {
            "symbol": "AAA",
            "source": "alpaca_sip",
            "volume": 1_000,
            "timestamp": datetime(2025, 6, 3, 15, 0, tzinfo=UTC),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.25,
        }
    )

    # Mapping access, because `forward_return_ladder` is untouched.
    assert bar["timestamp"] == datetime(2025, 6, 3, 15, 0, tzinfo=UTC)
    assert bar["close"] == 100.25
    assert bar.get("high") == 100.5
    assert bar.get("minutes_to_close") is None
    with pytest.raises(KeyError):
        bar["volume"]
    assert not hasattr(bar, "__dict__"), "a per-row dict is the cost being removed"


# ---------------------------------------------------------------------------
# Equivalence: the measurement is unchanged
# ---------------------------------------------------------------------------


def _eager_ladders(store: dict) -> dict[tuple[str, datetime], dict]:
    """The same ladders, computed the plainly-resident way.

    Everything is grouped up front from the raw fixture and handed to
    `forward_return_ladder` as ordinary dicts -- the shape the loader used
    before it streamed. If the streaming path had changed any measured number,
    this would disagree.
    """
    by_session: dict[str, dict[date, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in store["candles"]:
        if row["timeframe"] != "1m":
            continue
        by_session[row["symbol"]][exchange_session_date(row["timestamp"])].append(row)
    for sessions in by_session.values():
        for bars in sessions.values():
            bars.sort(key=lambda item: item["timestamp"])

    ladders: dict[tuple[str, datetime], dict] = {}
    for row in store["trade_flow"]:
        symbol = row["symbol"]
        session = exchange_session_date(row["timestamp"])
        decision = row["timestamp"] + timedelta(minutes=30)
        close = _session_close(session)
        ladders[(symbol, decision)] = forward_return_ladder(
            by_session[symbol][session],
            decision_timestamp=decision,
            horizons_seconds=HORIZONS,
            grid_seconds=60,
            session_close_timestamp=close,
        )
    return ladders


def test_the_streamed_panel_measures_exactly_what_an_eager_grid_measures():
    store = _store()
    conn = StreamingConn(store)
    panel = _panel(conn)

    report = attach_forward_returns(
        panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )

    expected = _eager_ladders(store)
    assert len(panel["observations"]) == len(expected)
    for observation in panel["observations"]:
        key = (observation["symbol"], observation["timestamp"])
        assert observation["forward"] == expected[key]
    # Not vacuous: the fixture really did resolve rungs.
    assert report["available_by_horizon"] == {
        "60s": len(expected),
        "300s": len(expected),
        "1800s": len(expected),
    }
    assert report["unavailable_by_reason"] == {}


def test_the_walk_order_does_not_change_the_report():
    """`attach_forward_returns` reorders the panel; it must not re-measure it."""
    store = _store()
    forward_panel = _panel(StreamingConn(store))
    reversed_panel = _panel(StreamingConn(store))
    reversed_panel["observations"].reverse()

    forward_report = attach_forward_returns(
        forward_panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )
    reversed_report = attach_forward_returns(
        reversed_panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )

    assert forward_report == reversed_report
    by_key = {
        (row["symbol"], row["timestamp"]): row["forward"]
        for row in reversed_panel["observations"]
    }
    for observation in forward_panel["observations"]:
        assert by_key[(observation["symbol"], observation["timestamp"])] == (
            observation["forward"]
        )


# ---------------------------------------------------------------------------
# Coverage semantics survive the presence pass
# ---------------------------------------------------------------------------


def test_a_session_with_no_frozen_grid_bars_is_skipped_by_name():
    """The presence pass replaced a `grid_by_session` lookup; the skip stands.

    Panel construction no longer holds the bars, so this counter is now derived
    from a two-column pass. It has to mean exactly what it meant before, or a
    dataset with a hole in its outcome grid would quietly produce observations
    whose ladders are all unavailable instead of an honest skip count.
    """
    store = _store()
    dropped = SESSIONS[1]
    store["candles"] = [
        row
        for row in store["candles"]
        if not (
            row["symbol"] == "BBB"
            and row["timeframe"] == "1m"
            and exchange_session_date(row["timestamp"]) == dropped
        )
    ]

    panel = _panel(StreamingConn(store))

    assert panel["skipped"] == {"no_grid_bars_for_session": len(SIGNAL_OPENS_ET_OFFSET)}
    assert not [
        row
        for row in panel["observations"]
        if row["symbol"] == "BBB" and row["session_date"] == dropped
    ]


def test_a_dataset_with_no_frozen_grid_at_all_is_refused_by_name():
    store = _store()
    store["candles"] = [row for row in store["candles"] if row["timeframe"] != "1m"]

    with pytest.raises(ValueError, match="no frozen 1m candles"):
        _panel(StreamingConn(store))


def test_the_grid_reads_only_frozen_tables():
    conn = StreamingConn(_store())
    panel = _panel(conn)
    attach_forward_returns(
        panel["observations"], horizons_seconds=HORIZONS, grid_seconds=60
    )

    assert conn.queries
    for query in conn.queries:
        assert "research_dataset_" in query
        assert "FROM candles" not in query
        assert "FROM intraday_trade_flow_features" not in query
        assert "dataset_id = %s" in query
