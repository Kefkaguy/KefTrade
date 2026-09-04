"""Ownership persistence, against a real transactional engine.

The rest of this repository's database tests use stub connections that match on
query substrings. A stub cannot fail a unique constraint, cannot roll back, and
agrees with whatever it is told -- so it cannot prove the two properties this
module exists to guarantee: that a fill applies exactly once, and that the event
row and the aggregate move together or not at all.

So these tests run the repository's real SQL against SQLite, which has real
transactions, real unique constraints and real rollback. The schema below is
migration 081 translated to SQLite types; the repository's statements are
unmodified. What this does not prove is PostgreSQL-specific DDL behaviour --
NUMERIC precision, the foreign keys, the CHECK constraints -- which still needs
the migration applied on a real database.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.strategy_ownership_repository import (
    OwnershipPersistenceError,
    apply_ownership_for_strategy,
    apply_ownership_from_fills,
    attributed_strategies,
    load_attributed_fills,
    load_attributions,
    load_ownership_state,
    ownership_drift,
    rebuild_ownership_from_events,
    reconstruct_missing_order_aggregate_fills,
    record_order_attribution,
    record_plan_attributions,
    replay_ownership_from_fills,
    verify_ownership_against_replay,
)

MOM = "MOM_12_1"
OTHER = "MEANREV_5"
ACCOUNT = 1
T0 = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)

SCHEMA = """
CREATE TABLE broker_orders (
    broker_account_id INTEGER NOT NULL,
    broker_order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    sync_run_id INTEGER NOT NULL DEFAULT 1,
    raw_event_id INTEGER NOT NULL DEFAULT 1,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_quantity TEXT,
    filled_quantity TEXT,
    filled_average_price TEXT,
    status TEXT,
    submitted_at TIMESTAMP,
    filled_at TIMESTAMP,
    updated_at TIMESTAMP,
    PRIMARY KEY (broker_account_id, broker_order_id)
);
CREATE TABLE broker_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_account_id INTEGER NOT NULL,
    broker_order_id TEXT NOT NULL,
    broker_activity_id TEXT NOT NULL,
    sync_run_id INTEGER NOT NULL DEFAULT 1,
    raw_event_id INTEGER NOT NULL DEFAULT 1,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    cumulative_quantity TEXT,
    leaves_quantity TEXT,
    source TEXT NOT NULL DEFAULT 'alpaca_account_activity',
    reconstructed BOOLEAN NOT NULL DEFAULT FALSE,
    transaction_at TIMESTAMP NOT NULL,
    UNIQUE (broker_account_id, broker_activity_id)
);
CREATE TABLE strategy_order_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_account_id INTEGER NOT NULL,
    client_order_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    intended_side TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE (broker_account_id, client_order_id)
);
CREATE TABLE strategy_ownership_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_account_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    side TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    quantity_delta TEXT NOT NULL,
    resulting_quantity TEXT NOT NULL,
    sync_run_id INTEGER,
    transaction_at TIMESTAMP NOT NULL,
    applied_at TIMESTAMP NOT NULL,
    UNIQUE (broker_account_id, fill_id)
);
CREATE TABLE strategy_owned_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    broker_account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    average_entry_price TEXT,
    as_of TIMESTAMP NOT NULL,
    reconciliation_run_id INTEGER,
    source TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE (strategy, broker_account_id, symbol)
);
"""


class Result:
    """A psycopg-shaped result: dict rows and a rowcount."""

    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchall(self):
        return [dict(row) for row in self._cursor.fetchall()]

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row else None


class Conn:
    """psycopg's surface over SQLite: %s parameters, real transactions."""

    def __init__(self, database=":memory:"):
        self._db = sqlite3.connect(database, isolation_level="DEFERRED")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()
        self.executed: list[str] = []

    def execute(self, query, params=()):
        self.executed.append(query)
        # Decimal and datetime are what the repository passes; SQLite wants text.
        adapted = tuple(
            str(p) if isinstance(p, (Decimal, datetime)) else p for p in params
        )
        return Result(self._db.execute(query.replace("%s", "?"), adapted))

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()

    def close(self):
        self._db.close()

    # -- helpers used only to arrange fixtures ------------------------------
    def owned(self, symbol, strategy=MOM):
        row = self.execute(
            "SELECT quantity FROM strategy_owned_positions "
            "WHERE strategy=%s AND broker_account_id=%s AND symbol=%s",
            (strategy, ACCOUNT, symbol),
        ).fetchone()
        return Decimal(row["quantity"]) if row else Decimal(0)

    def event_count(self, strategy=MOM):
        return self.execute(
            "SELECT COUNT(*) AS n FROM strategy_ownership_events "
            "WHERE broker_account_id=%s AND strategy=%s",
            (ACCOUNT, strategy),
        ).fetchone()["n"]

    def all_owned(self, strategy=MOM):
        rows = self.execute(
            "SELECT symbol, quantity FROM strategy_owned_positions "
            "WHERE strategy=%s AND broker_account_id=%s ORDER BY symbol",
            (strategy, ACCOUNT),
        ).fetchall()
        return {r["symbol"]: Decimal(r["quantity"]) for r in rows}


@pytest.fixture
def conn():
    connection = Conn()
    yield connection
    connection.close()


def add_order(
    conn, order_id, client_order_id, symbol="AAPL", side="buy", status="filled"
):
    conn.execute(
        "INSERT INTO broker_orders(broker_account_id, broker_order_id, client_order_id, "
        "symbol, side, requested_quantity, filled_quantity, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (ACCOUNT, order_id, client_order_id, symbol, side, "999", "0", status),
    )


def add_fill(
    conn,
    activity_id,
    order_id,
    symbol="AAPL",
    side="buy",
    qty="10",
    price="100",
    at=None,
):
    conn.execute(
        "INSERT INTO broker_fills(broker_account_id, broker_order_id, broker_activity_id, "
        "symbol, side, quantity, price, transaction_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (ACCOUNT, order_id, activity_id, symbol, side, qty, price, at or T0),
    )


def attribute(conn, client_order_id, *, strategy=MOM, symbol="AAPL", side="buy"):
    return record_order_attribution(
        conn,
        broker_account_id=ACCOUNT,
        client_order_id=client_order_id,
        strategy=strategy,
        strategy_version="1.0.0",
        symbol=symbol,
        intended_side=side,
        now=T0,
    )


def arrange_buy(
    conn,
    *,
    qty="10",
    client_order_id="kt-mom-1",
    order_id="o1",
    activity_id="act-1",
    symbol="AAPL",
    at=None,
):
    attribute(conn, client_order_id, symbol=symbol)
    add_order(conn, order_id, client_order_id, symbol=symbol)
    add_fill(conn, activity_id, order_id, symbol=symbol, qty=qty, at=at)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_an_attribution_row_is_written_once_for_a_deterministic_id(conn):
    assert attribute(conn, "kt-mom_12_1-abc") is True
    assert attribute(conn, "kt-mom_12_1-abc") is False  # idempotent
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM strategy_order_attributions", ()
    ).fetchone()["n"]
    assert count == 1


def test_preparing_the_same_rebalance_twice_attributes_the_same_orders(conn):
    attribute(conn, "kt-mom_12_1-aaa", symbol="AAPL")
    attribute(conn, "kt-mom_12_1-bbb", symbol="MSFT")
    again = [
        attribute(conn, "kt-mom_12_1-aaa", symbol="AAPL"),
        attribute(conn, "kt-mom_12_1-bbb", symbol="MSFT"),
    ]

    assert again == [False, False]
    loaded = load_attributions(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert set(loaded) == {"kt-mom_12_1-aaa", "kt-mom_12_1-bbb"}


def test_the_attribution_table_stores_no_quantity(conn):
    attribute(conn, "kt-mom-1")
    row = conn.execute("SELECT * FROM strategy_order_attributions", ()).fetchone()
    assert not any("quantity" in key or "notional" in key for key in row)
    assert set(row) >= {
        "broker_account_id",
        "client_order_id",
        "strategy",
        "strategy_version",
        "symbol",
        "intended_side",
    }


def test_an_order_without_a_client_order_id_cannot_be_attributed(conn):
    with pytest.raises(OwnershipPersistenceError, match="client order id"):
        attribute(conn, "")


def test_attributed_strategies_are_listed_for_the_account(conn):
    attribute(conn, "kt-mom-1", strategy=MOM)
    attribute(conn, "kt-other-1", strategy=OTHER)
    # Alphabetical, so the order a sync applies strategies in is stable.
    assert attributed_strategies(conn, broker_account_id=ACCOUNT) == sorted(
        [MOM, OTHER]
    )


def test_a_plan_is_attributed_without_persisting_any_quantity(conn, tmp_path):
    import csv

    from app.services.fractional_execution import AssetFact
    from app.services.portfolio_execution_bridge import (
        PROVENANCE_TEST_REPLAY,
        build_rebalance_plan,
        load_portfolio_signal,
    )
    from app.services.strategy_ownership import (
        ReconciliationEvidence,
        StrategyOwnershipLedger,
    )

    symbols = [f"SYM{i:04d}" for i in range(3)]
    path = tmp_path / "signal.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "symbol",
                "weight",
                "signal_date",
                "intended_execution_date",
                "strategy",
                "strategy_version",
                "universe_hash",
                "selected_count",
            ]
        )
        for symbol in symbols:
            writer.writerow(
                [
                    symbol,
                    "0.333333333",
                    "2026-07-31",
                    "2026-08-03",
                    MOM,
                    "1.0.0",
                    "f7b50c2b0c0882df",
                    3,
                ]
            )
    plan = build_rebalance_plan(
        signal=load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY),
        allocated_capital=Decimal(30000),
        reference_prices=dict.fromkeys(symbols, Decimal(100)),
        asset_facts={
            s: AssetFact(symbol=s, tradable=True, fractionable=True) for s in symbols
        },
        ownership=StrategyOwnershipLedger(
            strategy=MOM, positions={}, available=True, source="t"
        ),
        reconciliation=ReconciliationEvidence(
            run_id=1,
            status="clean",
            completed_at=datetime.now(UTC) - timedelta(minutes=1),
            broker_account_id=ACCOUNT,
        ),
    )
    first = record_plan_attributions(conn, plan, broker_account_id=ACCOUNT, now=T0)
    second = record_plan_attributions(conn, plan, broker_account_id=ACCOUNT, now=T0)

    assert first["attributions_written"] == 3
    assert second["attributions_written"] == 0  # idempotent re-preparation
    assert first["quantity_persisted"] is False
    assert conn.owned("SYM0000") == Decimal(0)  # planning owns nothing


# ---------------------------------------------------------------------------
# Applying confirmed fills
# ---------------------------------------------------------------------------


def test_a_confirmed_buy_fill_creates_ownership(conn):
    arrange_buy(conn, qty="10")
    result = apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)

    assert result["fills_applied"] == 1
    assert conn.owned("AAPL") == Decimal(10)
    assert conn.event_count() == 1


def test_a_partial_buy_fill_credits_only_the_filled_quantity(conn):
    # The order asked for 999; the market gave 30.
    arrange_buy(conn, qty="30")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert conn.owned("AAPL") == Decimal(30)


def test_multiple_partial_fills_aggregate(conn):
    attribute(conn, "kt-mom-1")
    add_order(conn, "o1", "kt-mom-1")
    add_fill(conn, "act-1", "o1", qty="30", at=T0)
    add_fill(conn, "act-2", "o1", qty="45", at=T0 + timedelta(seconds=1))
    add_fill(conn, "act-3", "o1", qty="25", at=T0 + timedelta(seconds=2))

    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert conn.owned("AAPL") == Decimal(100)
    assert conn.event_count() == 3


def test_a_partial_sell_fill_reduces_by_the_filled_quantity(conn):
    arrange_buy(conn, qty="100")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)

    attribute(conn, "kt-mom-2", side="sell")
    add_order(conn, "o2", "kt-mom-2", side="sell")
    add_fill(conn, "act-2", "o2", side="sell", qty="40", at=T0 + timedelta(hours=1))
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)

    assert conn.owned("AAPL") == Decimal(60)


def test_a_duplicate_activity_id_is_a_no_op(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    # The same activity arriving again -- an overlapping page from the broker.
    second = apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)

    assert second["fills_applied"] == 0
    assert second["fills_skipped"] == 1
    assert conn.owned("AAPL") == Decimal(10)
    assert conn.event_count() == 1


def test_two_sync_cycles_with_the_same_fills_produce_identical_ownership(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT, sync_run_id=1)
    after_first = conn.all_owned()
    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT, sync_run_id=2)

    assert conn.all_owned() == after_first == {"AAPL": Decimal(10)}
    assert conn.event_count() == 1


def test_a_second_cycle_applies_only_the_new_fills(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT, sync_run_id=1)

    add_fill(conn, "act-2", "o1", qty="7", at=T0 + timedelta(seconds=5))
    result = apply_ownership_from_fills(conn, broker_account_id=ACCOUNT, sync_run_id=2)

    assert result["fills_applied"] == 1
    assert conn.owned("AAPL") == Decimal(17)


def test_fractional_quantities_round_trip_through_the_tables(conn):
    arrange_buy(conn, qty="1.234567891")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert conn.owned("AAPL") == Decimal("1.234567891")


# ---------------------------------------------------------------------------
# What must never create ownership
# ---------------------------------------------------------------------------


def test_an_unattributed_manual_fill_moves_nothing(conn):
    # An order placed by hand: it exists at the broker, nobody claimed it.
    add_order(conn, "o-manual", "typed-by-hand")
    add_fill(conn, "act-manual", "o-manual", qty="500")

    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT)
    assert conn.all_owned() == {}
    assert conn.event_count() == 0


def test_another_strategys_fill_never_moves_this_strategys_ownership(conn):
    arrange_buy(conn, qty="20")
    attribute(conn, "kt-other-1", strategy=OTHER)
    add_order(conn, "o-other", "kt-other-1")
    add_fill(conn, "act-other", "o-other", qty="80", at=T0 + timedelta(seconds=1))

    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT)

    assert conn.owned("AAPL", MOM) == Decimal(20)
    assert conn.owned("AAPL", OTHER) == Decimal(80)


def test_two_strategies_share_a_symbol_without_sharing_a_book(conn):
    arrange_buy(conn, qty="20")
    attribute(conn, "kt-other-1", strategy=OTHER)
    add_order(conn, "o-other", "kt-other-1")
    add_fill(conn, "act-other", "o-other", qty="80", at=T0 + timedelta(seconds=1))
    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT)

    # The account holds 100. Neither strategy may claim the other's share.
    assert conn.owned("AAPL", MOM) + conn.owned("AAPL", OTHER) == Decimal(100)
    assert load_ownership_state(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    ).owned_quantity("AAPL") == Decimal(20)


def test_a_sell_cannot_consume_another_strategys_shares(conn):
    """MOM owns 20 of the account's 100. A sell fill of 50 attributed to MOM is
    not a rebalance -- it is 30 shares of someone else's position."""
    arrange_buy(conn, qty="20")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()  # MOM's 20 shares are settled fact before the bad sell arrives

    attribute(conn, "kt-mom-2", side="sell")
    add_order(conn, "o2", "kt-mom-2", side="sell")
    add_fill(conn, "act-2", "o2", side="sell", qty="50", at=T0 + timedelta(hours=1))
    conn.commit()

    with pytest.raises(OwnershipPersistenceError, match="clamping"):
        apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)

    conn.rollback()
    assert conn.owned("AAPL") == Decimal(20)  # not 0, not -30


def test_an_order_status_alone_creates_no_ownership(conn):
    """A filled-looking order with no fill activity behind it."""
    attribute(conn, "kt-mom-1")
    add_order(conn, "o1", "kt-mom-1", status="filled")
    conn.execute(
        "UPDATE broker_orders SET filled_quantity=%s WHERE broker_order_id=%s",
        ("100", "o1"),
    )

    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT)
    assert conn.all_owned() == {}


def test_a_cancelled_order_creates_no_ownership(conn):
    attribute(conn, "kt-mom-1")
    add_order(conn, "o1", "kt-mom-1", status="canceled")
    apply_ownership_from_fills(conn, broker_account_id=ACCOUNT)
    assert conn.all_owned() == {}
    assert conn.event_count() == 0


def test_requested_quantity_is_never_read(conn):
    """The order asks for 999 and fills 10. Ownership follows the fill."""
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert conn.owned("AAPL") == Decimal(10)

    import inspect

    from app.services import strategy_ownership_repository as repo

    # The repository's own SQL, not the fixtures': requested quantity and the
    # account position book are never selected from.
    source = inspect.getsource(repo)
    assert "requested_quantity" not in source
    assert "broker_positions" not in source
    assert (
        "filled_quantity" not in source.split("CREATE")[0].split("broker_orders o")[0]
    )


# ---------------------------------------------------------------------------
# Atomicity and restart
# ---------------------------------------------------------------------------


def test_the_event_and_the_aggregate_commit_together(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    assert conn.event_count() == 1
    assert conn.owned("AAPL") == Decimal(10)


def test_a_rollback_leaves_neither_the_event_nor_the_aggregate(conn):
    """The crash case. If the transaction dies after the event insert and before
    the commit, a restart must find no trace of either."""
    arrange_buy(conn, qty="10")
    conn.commit()

    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.rollback()  # the process dies here

    assert conn.event_count() == 0
    assert conn.owned("AAPL") == Decimal(0)

    # And the restart applies the fill exactly once.
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()
    assert conn.event_count() == 1
    assert conn.owned("AAPL") == Decimal(10)


def test_a_crash_after_commit_does_not_re_apply_on_restart(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    # Restart: a fresh read of the same tables, same fills still present.
    restarted = apply_ownership_for_strategy(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    )
    conn.commit()

    assert restarted["fills_applied"] == 0
    assert conn.owned("AAPL") == Decimal(10)
    assert conn.event_count() == 1


def test_the_unique_constraint_is_what_stops_a_double_apply(conn):
    """Not merely the in-memory set: the database refuses it too."""
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO strategy_ownership_events(broker_account_id, strategy, symbol, "
            "fill_id, broker_order_id, client_order_id, side, filled_quantity, fill_price, "
            "quantity_delta, resulting_quantity, transaction_at, applied_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                ACCOUNT,
                MOM,
                "AAPL",
                "act-1",
                "o1",
                "kt-mom-1",
                "buy",
                "10",
                "100",
                "10",
                "20",
                T0,
                T0,
            ),
        )


# ---------------------------------------------------------------------------
# Replay and drift
# ---------------------------------------------------------------------------


def test_incremental_processing_equals_replay(conn):
    attribute(conn, "kt-mom-1")
    attribute(conn, "kt-mom-2", symbol="MSFT")
    add_order(conn, "o1", "kt-mom-1")
    add_order(conn, "o2", "kt-mom-2", symbol="MSFT")
    add_fill(conn, "a1", "o1", qty="10", at=T0)
    add_fill(conn, "a2", "o2", symbol="MSFT", qty="5", at=T0 + timedelta(seconds=1))
    add_fill(conn, "a3", "o1", side="sell", qty="4", at=T0 + timedelta(seconds=2))
    add_fill(conn, "a4", "o1", qty="2.5", at=T0 + timedelta(seconds=3))

    # One fill per cycle, the way sync would see them arrive.
    for _ in range(4):
        apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    replayed = replay_ownership_from_fills(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    )
    stored = conn.all_owned()
    assert stored == {"AAPL": Decimal("8.5"), "MSFT": Decimal(5)}
    assert {s: p.quantity for s, p in replayed.positions.items()} == stored


def test_the_event_log_rebuilds_the_same_aggregate(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    assert rebuild_ownership_from_events(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    ) == {"AAPL": Decimal(10)}


def test_verification_reports_agreement_without_writing(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    report = verify_ownership_against_replay(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    )
    assert report["matches"] is True
    assert report["differences"] == []
    assert report["rows_written"] == 0


def test_drift_between_the_aggregate_and_the_event_log_is_detected(conn):
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()

    # Someone edits the aggregate outside this code path.
    conn.execute(
        "UPDATE strategy_owned_positions SET quantity=%s WHERE symbol=%s",
        ("999", "AAPL"),
    )
    conn.commit()

    drift = ownership_drift(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert drift and drift[0]["symbol"] == "AAPL"
    assert Decimal(drift[0]["expected_from_events"]) == Decimal(10)
    assert Decimal(drift[0]["stored_quantity"]) == Decimal(999)


def test_drift_blocks_further_writes_rather_than_being_overwritten(conn):
    """The aggregate is a cache of the event log. Quietly rewriting it to match
    would destroy the only evidence of how it diverged."""
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()
    conn.execute(
        "UPDATE strategy_owned_positions SET quantity=%s WHERE symbol=%s",
        ("999", "AAPL"),
    )
    conn.commit()

    add_fill(conn, "act-2", "o1", qty="5", at=T0 + timedelta(seconds=5))
    with pytest.raises(OwnershipPersistenceError, match="STORED_OWNERSHIP_DISAGREES"):
        apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)

    conn.rollback()
    assert conn.owned("AAPL") == Decimal(999)  # untouched, still visibly wrong


def test_replay_ignores_the_stored_aggregate_entirely(conn):
    """Otherwise agreement between the two would be a tautology."""
    arrange_buy(conn, qty="10")
    apply_ownership_for_strategy(conn, broker_account_id=ACCOUNT, strategy=MOM)
    conn.commit()
    conn.execute(
        "UPDATE strategy_owned_positions SET quantity=%s WHERE symbol=%s",
        ("999", "AAPL"),
    )
    conn.commit()

    replayed = replay_ownership_from_fills(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    )
    assert replayed.owned_quantity("AAPL") == Decimal(10)
    report = verify_ownership_against_replay(
        conn, broker_account_id=ACCOUNT, strategy=MOM
    )
    assert report["matches"] is False
    assert Decimal(report["differences"][0]["stored_quantity"]) == Decimal(999)


def test_only_attributed_fills_reach_the_replay(conn):
    arrange_buy(conn, qty="10")
    add_order(conn, "o-manual", "typed-by-hand")
    add_fill(conn, "act-manual", "o-manual", qty="500", at=T0 + timedelta(seconds=1))

    fills = load_attributed_fills(conn, broker_account_id=ACCOUNT, strategy=MOM)
    assert [f.fill_id for f in fills] == ["act-1"]


def test_filled_attributed_order_recovers_when_activity_is_missing(conn):
    attribute(conn, "kt-rsi5-1", strategy="SPY_RSI5_SMA200", symbol="SPY")
    add_order(conn, "order-spy", "kt-rsi5-1", symbol="SPY")
    conn.execute(
        "UPDATE broker_orders SET filled_quantity=%s, filled_average_price=%s, "
        "filled_at=%s, updated_at=%s WHERE broker_order_id=%s",
        ("1.311531096", "762.46", T0, T0, "order-spy"),
    )

    recovery = reconstruct_missing_order_aggregate_fills(
        conn, broker_account_id=ACCOUNT, sync_run_id=77
    )
    result = apply_ownership_for_strategy(
        conn,
        broker_account_id=ACCOUNT,
        strategy="SPY_RSI5_SMA200",
        strategy_version="1.0.0",
        sync_run_id=77,
        now=T0,
    )

    assert recovery["fills_reconstructed"] == 1
    assert result["fills_applied"] == 1
    assert conn.owned("SPY", strategy="SPY_RSI5_SMA200") == Decimal("1.311531096")
    fill = conn.execute(
        "SELECT * FROM broker_fills WHERE broker_order_id=%s", ("order-spy",)
    ).fetchone()
    assert fill["source"] == "order_aggregate_reconstruction"
    assert fill["reconstructed"] == 1


def test_one_off_manual_recovery_is_adopted_only_when_broker_evidence_matches(conn):
    strategy = "SPY_RSI5_SMA200"
    attribute(conn, "kt-rsi5-1", strategy=strategy, symbol="SPY")
    add_order(conn, "order-spy", "kt-rsi5-1", symbol="SPY")
    conn.execute(
        "UPDATE broker_orders SET filled_quantity=%s, filled_average_price=%s, "
        "filled_at=%s, updated_at=%s WHERE broker_order_id=%s",
        ("1.311531096", "762.46", T0, T0, "order-spy"),
    )
    add_fill(
        conn,
        "alpaca-fill-spy",
        "order-spy",
        symbol="SPY",
        qty="1.311531096",
        price="762.46",
        at=T0,
    )
    conn.execute(
        """INSERT INTO strategy_owned_positions(
               strategy, strategy_version, broker_account_id, symbol, quantity,
               average_entry_price, as_of, reconciliation_run_id, source,
               created_at, updated_at
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            strategy,
            "1.0.0",
            ACCOUNT,
            "SPY",
            "1.311531096",
            "762.46",
            T0,
            None,
            "recovered_from_attributed_filled_order",
            T0,
            T0,
        ),
    )

    result = apply_ownership_for_strategy(
        conn,
        broker_account_id=ACCOUNT,
        strategy=strategy,
        strategy_version="1.0.0",
        sync_run_id=77,
        now=T0,
    )

    assert result["fills_applied"] == 0
    assert result["fills_skipped"] == 1
    assert conn.event_count(strategy=strategy) == 1
    assert ownership_drift(conn, broker_account_id=ACCOUNT, strategy=strategy) == []
    stored = conn.execute(
        "SELECT source FROM strategy_owned_positions WHERE strategy=%s AND symbol=%s",
        (strategy, "SPY"),
    ).fetchone()
    assert stored["source"] == "recovered_from_attributed_filled_order_verified"


# ---------------------------------------------------------------------------
# The sync wiring
# ---------------------------------------------------------------------------


def test_broker_sync_applies_ownership_inside_its_transaction():
    """Wired after persist_fills and before the sync commit, so ownership and
    the evidence it came from land in the same commit."""
    import inspect

    from app.services import broker_sync

    source = inspect.getsource(broker_sync.persist_normalized_state)
    assert source.index("persist_fills(") < source.index("apply_strategy_ownership(")

    hook = inspect.getsource(broker_sync.apply_strategy_ownership)
    assert "apply_ownership_from_fills" in hook
    assert ".commit()" not in hook  # the sync transaction owns the commit

    cycle = inspect.getsource(broker_sync.synchronize_broker)
    # persist_normalized_state runs inside the try, and the single commit that
    # follows is what makes the ownership write atomic with the sync.
    assert cycle.index("persist_normalized_state(") < cycle.index(
        "conn.commit()", cycle.index("persist_normalized_state(")
    )


def test_the_sync_hook_reaches_no_broker_and_no_position_book():
    import inspect

    from app.services import strategy_ownership_repository as repo

    source = inspect.getsource(repo)
    assert "broker_positions" not in source
    assert "httpx" not in source
    assert "/v2/" not in source


def test_the_repository_never_commits_on_its_own():
    """Committing inside would break the atomicity the caller depends on."""
    import inspect

    from app.services import strategy_ownership_repository as repo

    for name in (
        "apply_ownership_for_strategy",
        "apply_ownership_from_fills",
        "record_order_attribution",
        "_persist_event",
        "_upsert_position",
    ):
        source = inspect.getsource(getattr(repo, name))
        assert ".commit()" not in source
        assert ".rollback()" not in source
