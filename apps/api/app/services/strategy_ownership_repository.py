"""Persistence for strategy ownership: attribution in, confirmed fills applied.

``strategy_ownership_lifecycle`` owns the arithmetic and knows nothing about a
database. This module is the other half: it reads the tables migration 081
defines, hands rows to that arithmetic, and writes back what it returns.

Two rules shape every function here.

**The event log is the source of truth, and the aggregate is a cache of it.**
``strategy_ownership_events`` holds one immutable row per applied fill, keyed on
the broker's own activity id. ``strategy_owned_positions`` is the sum of those
rows. Whenever the two disagree, the aggregate is wrong by definition -- so a
disagreement faults rather than being overwritten, because silently rewriting a
cache to match nothing in particular destroys the evidence of what went wrong.

**Nothing here commits.** Every function runs inside the caller's transaction,
which is what makes "insert the event and update the aggregate together"
meaningful. A crash between the two must roll back both, or a restart would
re-apply a fill whose effect is already in the aggregate.

The SQL is deliberately written in the subset PostgreSQL and SQLite share --
positional parameters, ``ON CONFLICT``, no server-side ``NOW()`` -- so the
repository can be exercised against a real transactional engine in tests rather
than a stub that agrees with whatever it is told.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.services.strategy_ownership_lifecycle import (
    APPLIED,
    AttributedPosition,
    ConfirmedFill,
    OrderAttribution,
    OwnershipState,
    apply_confirmed_fill,
)

DRIFT_FAULT = "STORED_OWNERSHIP_DISAGREES_WITH_EVENT_LOG"
STORED_QUANTITY_PRECISION = Decimal("0.000000001")  # NUMERIC(20, 9)


class OwnershipPersistenceError(RuntimeError):
    """The stored ledger cannot be trusted, so nothing may be written on top."""


def _rows(result: Any) -> list[dict[str, Any]]:
    fetched = result.fetchall() if result is not None else []
    return [dict(row) for row in (fetched or [])]


def _decimal(value: Any) -> Decimal:
    """Postgres hands back Decimal; SQLite hands back whatever went in."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _at_stored_precision(value: Decimal) -> Decimal:
    """Quantize to NUMERIC(20, 9), the precision the tables actually hold.

    Comparing two quantities beyond the precision either can be stored at
    compares noise, and noise here would read as ledger drift.
    """
    return value.quantize(STORED_QUANTITY_PRECISION)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def record_order_attribution(
    conn: Any,
    *,
    broker_account_id: int,
    client_order_id: str,
    strategy: str,
    strategy_version: str,
    symbol: str,
    intended_side: str,
    now: datetime | None = None,
) -> bool:
    """Record whose order this is. Returns True if a new row was written.

    Idempotent on ``(broker_account_id, client_order_id)``. Client order ids are
    deterministic per (strategy, version, rebalance, symbol), so preparing the
    same rebalance twice re-attributes the same orders instead of claiming a
    second set.

    Carries no quantity, deliberately. The moment attribution recorded how much
    was asked for, submitted size would be one join away from being mistaken for
    what the market actually gave us.
    """
    if not client_order_id:
        raise OwnershipPersistenceError(
            "an order cannot be attributed without a client order id; an "
            "unattributable order can never be credited to a strategy"
        )
    if intended_side not in ("buy", "sell"):
        raise OwnershipPersistenceError(f"unknown intended side {intended_side!r}")

    result = conn.execute(
        """
        INSERT INTO strategy_order_attributions
            (broker_account_id, client_order_id, strategy, strategy_version,
             symbol, intended_side, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (broker_account_id, client_order_id) DO NOTHING
        """,
        (
            broker_account_id,
            client_order_id,
            strategy,
            strategy_version,
            symbol.upper(),
            intended_side,
            now or datetime.now(UTC),
        ),
    )
    return bool(getattr(result, "rowcount", 0))


def record_plan_attributions(
    conn: Any, plan: Any, *, broker_account_id: int, now: datetime | None = None
) -> dict[str, Any]:
    """Attribute every order a rebalance plan would send, before it is sent.

    Attribution must exist before submission, not after: a fill can arrive
    before the next sync cycle, and an unattributed fill is one nobody can
    credit to a strategy afterwards without guessing.
    """
    from app.services.strategy_ownership_lifecycle import attributions_for_plan

    rows = attributions_for_plan(plan, broker_account_id=broker_account_id)
    written = 0
    for row in rows:
        if record_order_attribution(
            conn,
            broker_account_id=row["broker_account_id"],
            client_order_id=row["client_order_id"],
            strategy=row["strategy"],
            strategy_version=row["strategy_version"],
            symbol=row["symbol"],
            intended_side=row["intended_side"],
            now=now,
        ):
            written += 1
    return {
        "attributions_seen": len(rows),
        "attributions_written": written,
        "quantity_persisted": False,
    }


def load_attributions(
    conn: Any, *, broker_account_id: int, strategy: str | None = None
) -> dict[str, OrderAttribution]:
    if strategy is None:
        result = conn.execute(
            """
            SELECT client_order_id, strategy, strategy_version, broker_account_id, symbol
              FROM strategy_order_attributions
             WHERE broker_account_id = %s
            """,
            (broker_account_id,),
        )
    else:
        result = conn.execute(
            """
            SELECT client_order_id, strategy, strategy_version, broker_account_id, symbol
              FROM strategy_order_attributions
             WHERE broker_account_id = %s AND strategy = %s
            """,
            (broker_account_id, strategy),
        )
    return {
        str(row["client_order_id"]): OrderAttribution(
            client_order_id=str(row["client_order_id"]),
            strategy=str(row["strategy"]),
            strategy_version=str(row["strategy_version"]),
            broker_account_id=int(row["broker_account_id"]),
            symbol=str(row["symbol"]).upper(),
        )
        for row in _rows(result)
    }


def attributed_strategies(conn: Any, *, broker_account_id: int) -> list[str]:
    """Which strategies have orders on this account, in a stable order."""
    result = conn.execute(
        """
        SELECT DISTINCT strategy FROM strategy_order_attributions
         WHERE broker_account_id = %s ORDER BY strategy
        """,
        (broker_account_id,),
    )
    return [str(row["strategy"]) for row in _rows(result)]


# ---------------------------------------------------------------------------
# Reading the ledger
# ---------------------------------------------------------------------------


def load_ownership_state(
    conn: Any, *, broker_account_id: int, strategy: str, strategy_version: str = "unknown"
) -> OwnershipState:
    """The stored aggregate, plus every fill id already applied to it.

    Both halves are loaded together because applying a fill requires knowing not
    only the current quantity but whether this particular fill is already inside
    it. Loading one without the other is how a restart double-counts.
    """
    state = OwnershipState(
        strategy=strategy,
        strategy_version=strategy_version,
        broker_account_id=broker_account_id,
    )
    positions = conn.execute(
        """
        SELECT symbol, quantity, average_entry_price, as_of, strategy_version,
               reconciliation_run_id
          FROM strategy_owned_positions
         WHERE broker_account_id = %s AND strategy = %s
        """,
        (broker_account_id, strategy),
    )
    for row in _rows(positions):
        symbol = str(row["symbol"]).upper()
        state.positions[symbol] = AttributedPosition(
            strategy=strategy,
            strategy_version=str(row["strategy_version"] or strategy_version),
            broker_account_id=broker_account_id,
            symbol=symbol,
            quantity=_decimal(row["quantity"]),
            average_entry_price=(
                _decimal(row["average_entry_price"])
                if row["average_entry_price"] is not None
                else None
            ),
            as_of=row["as_of"],
            reconciliation_run_id=row["reconciliation_run_id"],
        )
    applied = conn.execute(
        """
        SELECT fill_id FROM strategy_ownership_events
         WHERE broker_account_id = %s AND strategy = %s
        """,
        (broker_account_id, strategy),
    )
    state.applied_fill_ids = {str(row["fill_id"]) for row in _rows(applied)}
    return state


def load_attributed_fills(
    conn: Any, *, broker_account_id: int, strategy: str
) -> list[ConfirmedFill]:
    """Confirmed fills belonging to this strategy's orders, oldest first.

    Joined through ``broker_orders`` because ``broker_fills`` carries the broker
    order id, while attribution is keyed on the client order id we chose. A fill
    whose order we never attributed simply does not appear -- that is the manual
    and other-strategy case, and it is meant to be invisible here rather than
    filtered out later.
    """
    result = conn.execute(
        """
        SELECT f.broker_activity_id, f.broker_account_id, f.broker_order_id,
               o.client_order_id, f.symbol, f.side, f.quantity, f.price,
               f.transaction_at
          FROM broker_fills f
          JOIN broker_orders o
            ON o.broker_account_id = f.broker_account_id
           AND o.broker_order_id = f.broker_order_id
          JOIN strategy_order_attributions a
            ON a.broker_account_id = o.broker_account_id
           AND a.client_order_id = o.client_order_id
         WHERE f.broker_account_id = %s AND a.strategy = %s
         ORDER BY f.transaction_at, f.broker_activity_id
        """,
        (broker_account_id, strategy),
    )
    return [
        ConfirmedFill(
            fill_id=str(row["broker_activity_id"]),
            broker_account_id=int(row["broker_account_id"]),
            broker_order_id=str(row["broker_order_id"]),
            client_order_id=str(row["client_order_id"] or ""),
            symbol=str(row["symbol"]).upper(),
            side=str(row["side"]).lower(),
            quantity=_decimal(row["quantity"]),
            price=_decimal(row["price"]),
            transaction_at=row["transaction_at"],
            # broker_fills holds executions only; a row here is a fill by
            # construction, which is why the table has no status column.
            activity_type="fill",
        )
        for row in _rows(result)
    ]


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def ownership_drift(
    conn: Any, *, broker_account_id: int, strategy: str
) -> list[dict[str, Any]]:
    """Where the stored aggregate disagrees with the sum of applied events.

    Cheap enough to run every cycle: the events carry a signed delta each, so
    the aggregate is one GROUP BY away and needs no replay of the arithmetic.
    """
    summed = conn.execute(
        """
        SELECT symbol, SUM(quantity_delta) AS total
          FROM strategy_ownership_events
         WHERE broker_account_id = %s AND strategy = %s
         GROUP BY symbol
        """,
        (broker_account_id, strategy),
    )
    from_events = {
        str(row["symbol"]).upper(): _decimal(row["total"]) for row in _rows(summed)
    }
    stored_rows = conn.execute(
        """
        SELECT symbol, quantity FROM strategy_owned_positions
         WHERE broker_account_id = %s AND strategy = %s
        """,
        (broker_account_id, strategy),
    )
    stored = {
        str(row["symbol"]).upper(): _decimal(row["quantity"])
        for row in _rows(stored_rows)
    }

    drift: list[dict[str, Any]] = []
    for symbol in sorted(set(from_events) | set(stored)):
        expected = _at_stored_precision(from_events.get(symbol, Decimal(0)))
        actual = _at_stored_precision(stored.get(symbol, Decimal(0)))
        if expected != actual:
            drift.append(
                {
                    "symbol": symbol,
                    "expected_from_events": str(expected),
                    "stored_quantity": str(actual),
                }
            )
    return drift


def assert_ownership_integrity(
    conn: Any, *, broker_account_id: int, strategy: str
) -> None:
    """Refuse to write on top of a ledger that already disagrees with itself."""
    drift = ownership_drift(conn, broker_account_id=broker_account_id, strategy=strategy)
    if drift:
        raise OwnershipPersistenceError(
            f"{DRIFT_FAULT}: stored ownership for {strategy} does not match the "
            f"applied event log ({drift}); refusing to write on top of it, "
            "because overwriting the aggregate would destroy the only record of "
            "how it diverged"
        )


# ---------------------------------------------------------------------------
# Applying fills
# ---------------------------------------------------------------------------


def _persist_event(
    conn: Any,
    *,
    strategy: str,
    fill: ConfirmedFill,
    delta: Decimal,
    resulting: Decimal,
    sync_run_id: int | None,
    now: datetime,
) -> bool:
    """One immutable row per applied fill. Returns False if it was already there.

    The ``ON CONFLICT DO NOTHING`` is the crash guard. If a previous run wrote
    the event and died before committing the aggregate, the transaction rolled
    back and neither exists. If it committed, this returns False and the caller
    skips the fill -- so at no interleaving does one fill move the aggregate
    twice.
    """
    result = conn.execute(
        """
        INSERT INTO strategy_ownership_events
            (broker_account_id, strategy, symbol, fill_id, broker_order_id,
             client_order_id, side, filled_quantity, fill_price, quantity_delta,
             resulting_quantity, sync_run_id, transaction_at, applied_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (broker_account_id, fill_id) DO NOTHING
        """,
        (
            fill.broker_account_id,
            strategy,
            fill.symbol.upper(),
            fill.fill_id,
            fill.broker_order_id,
            fill.client_order_id,
            fill.side,
            fill.quantity,
            fill.price,
            delta,
            resulting,
            sync_run_id,
            fill.transaction_at,
            now,
        ),
    )
    return bool(getattr(result, "rowcount", 0))


def _upsert_position(
    conn: Any, held: AttributedPosition, *, now: datetime, source: str = "confirmed_fill"
) -> None:
    conn.execute(
        """
        INSERT INTO strategy_owned_positions
            (strategy, strategy_version, broker_account_id, symbol, quantity,
             average_entry_price, as_of, reconciliation_run_id, source,
             created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (strategy, broker_account_id, symbol) DO UPDATE SET
            strategy_version = excluded.strategy_version,
            quantity = excluded.quantity,
            average_entry_price = excluded.average_entry_price,
            as_of = excluded.as_of,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            held.strategy,
            held.strategy_version,
            held.broker_account_id,
            held.symbol.upper(),
            held.quantity,
            held.average_entry_price,
            held.as_of,
            held.reconciliation_run_id,
            source,
            now,
            now,
        ),
    )


def apply_ownership_for_strategy(
    conn: Any,
    *,
    broker_account_id: int,
    strategy: str,
    strategy_version: str = "unknown",
    sync_run_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply every attributed confirmed fill not yet applied. One transaction.

    Runs inside the caller's transaction and never commits. The event row and
    the aggregate update for a given fill are therefore written together or not
    at all, which is the whole of the restart guarantee.
    """
    stamp = now or datetime.now(UTC)
    assert_ownership_integrity(
        conn, broker_account_id=broker_account_id, strategy=strategy
    )

    state = load_ownership_state(
        conn,
        broker_account_id=broker_account_id,
        strategy=strategy,
        strategy_version=strategy_version,
    )
    attributions = load_attributions(
        conn, broker_account_id=broker_account_id, strategy=strategy
    )
    fills = load_attributed_fills(
        conn, broker_account_id=broker_account_id, strategy=strategy
    )

    applied = 0
    skipped = 0
    faults: list[dict[str, Any]] = []
    touched: set[str] = set()

    for fill in fills:
        if fill.fill_id in state.applied_fill_ids:
            skipped += 1
            continue
        result = apply_confirmed_fill(
            fill, attribution=attributions.get(fill.client_order_id), state=state
        )
        if result.status != APPLIED:
            if result.status not in ("duplicate", "unattributed"):
                faults.append(result.as_dict())
            else:
                skipped += 1
            continue
        # The event is written first so that a unique-violation here -- another
        # process got there in between -- prevents the aggregate moving at all.
        fresh = _persist_event(
            conn,
            strategy=strategy,
            fill=fill,
            delta=result.quantity_delta,
            resulting=result.resulting_quantity or Decimal(0),
            sync_run_id=sync_run_id,
            now=stamp,
        )
        if not fresh:
            # Already applied by someone else. Undo the in-memory move rather
            # than writing an aggregate that counts this fill twice.
            raise OwnershipPersistenceError(
                f"fill {fill.fill_id} was applied concurrently; the transaction "
                "is abandoned so the aggregate cannot count it twice"
            )
        touched.add(fill.symbol.upper())
        applied += 1

    for symbol in sorted(touched):
        _upsert_position(conn, state.positions[symbol], now=stamp)

    if faults:
        # Fail closed. The caller's transaction is expected to roll back, which
        # leaves both the events and the aggregate exactly as they were.
        raise OwnershipPersistenceError(
            f"ownership faults for {strategy}: {faults}; refusing to apply a "
            "partial batch, because a sell that exceeds attribution is someone "
            "else's shares and must not be absorbed by clamping"
        )

    return {
        "strategy": strategy,
        "broker_account_id": broker_account_id,
        "fills_considered": len(fills),
        "fills_applied": applied,
        "fills_skipped": skipped,
        "symbols_updated": sorted(touched),
        "faults": faults,
        "ownership_from_confirmed_fills_only": True,
    }


def apply_ownership_from_fills(
    conn: Any,
    *,
    broker_account_id: int,
    sync_run_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The broker-sync hook: apply new fills for every attributed strategy.

    Called after orders and fills are persisted and before the sync transaction
    commits, so ownership advances in the same commit as the evidence it was
    derived from. There is deliberately no second polling loop: a separate
    process reading the same fills would need its own idempotency story, and one
    is enough.
    """
    results = [
        apply_ownership_for_strategy(
            conn,
            broker_account_id=broker_account_id,
            strategy=strategy,
            sync_run_id=sync_run_id,
            now=now,
        )
        for strategy in attributed_strategies(conn, broker_account_id=broker_account_id)
    ]
    return {
        "strategies": results,
        "fills_applied": sum(r["fills_applied"] for r in results),
        "ownership_from_confirmed_fills_only": True,
    }


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def rebuild_ownership_from_events(
    conn: Any, *, broker_account_id: int, strategy: str
) -> dict[str, Decimal]:
    """The aggregate as the immutable event log implies it, ignoring what is stored."""
    result = conn.execute(
        """
        SELECT symbol, SUM(quantity_delta) AS total
          FROM strategy_ownership_events
         WHERE broker_account_id = %s AND strategy = %s
         GROUP BY symbol ORDER BY symbol
        """,
        (broker_account_id, strategy),
    )
    return {
        str(row["symbol"]).upper(): _decimal(row["total"]) for row in _rows(result)
    }


def replay_ownership_from_fills(
    conn: Any, *, broker_account_id: int, strategy: str, strategy_version: str = "unknown"
) -> OwnershipState:
    """Recompute from every attributed confirmed fill, as if nothing were stored.

    This is the specification the incremental path must agree with. It reads
    fills and attributions only -- never the aggregate, never the event log --
    so agreement between the two is real evidence rather than a tautology.
    """
    from app.services.strategy_ownership_lifecycle import replay_fills

    return replay_fills(
        load_attributed_fills(
            conn, broker_account_id=broker_account_id, strategy=strategy
        ),
        attributions=load_attributions(
            conn, broker_account_id=broker_account_id, strategy=strategy
        ),
        strategy=strategy,
        strategy_version=strategy_version,
        broker_account_id=broker_account_id,
    )


def verify_ownership_against_replay(
    conn: Any, *, broker_account_id: int, strategy: str
) -> dict[str, Any]:
    """Compare stored ownership to a full replay. Read-only; reports, never repairs.

    Repair is a separate decision for a person to make, because the two ways the
    aggregate can differ from the replay -- a bug in application, or a row edited
    outside this code path -- want opposite responses.
    """
    stored = load_ownership_state(
        conn, broker_account_id=broker_account_id, strategy=strategy
    )
    replayed = replay_ownership_from_fills(
        conn, broker_account_id=broker_account_id, strategy=strategy
    )
    symbols = sorted(set(stored.positions) | set(replayed.positions))
    differences = [
        {
            "symbol": symbol,
            "stored_quantity": str(stored.owned_quantity(symbol)),
            "replayed_quantity": str(replayed.owned_quantity(symbol)),
        }
        for symbol in symbols
        if stored.owned_quantity(symbol) != replayed.owned_quantity(symbol)
    ]
    return {
        "strategy": strategy,
        "broker_account_id": broker_account_id,
        "matches": not differences and not replayed.has_faults,
        "differences": differences,
        "replay_faults": list(replayed.faults),
        "event_log_drift": ownership_drift(
            conn, broker_account_id=broker_account_id, strategy=strategy
        ),
        "rows_written": 0,
    }
