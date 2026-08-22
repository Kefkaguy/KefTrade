"""How a strategy comes to own shares, and stops owning them.

``strategy_ownership`` answers "what does MOM_12_1 own" and refuses to guess.
This module answers the prior question: how that record comes to exist at all.

The rule the whole module enforces is that **only a confirmed fill moves
attribution**. An order we intended to send, an order we did send, an order
sitting open at the venue -- none of these are shares. A rebalance that credits
itself for a submitted order owns a position the market never gave it, and the
next rebalance sizes against shares that do not exist. So intent supplies
*attribution* (whose order this was) and confirmed fills supply *quantity*, and
the two are never allowed to swap roles.

Everything here is a pure function over explicit inputs. Persistence lives in
the caller, because the invariants worth testing -- idempotency, non-negativity,
replay determinism -- are properties of the arithmetic, not of the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.services.strategy_ownership import (
    StrategyOwnedPosition,
    StrategyOwnershipLedger,
)

QUANTITY_PRECISION = Decimal("0.000000001")  # NUMERIC(20, 9), as the table stores
PRICE_PRECISION = Decimal("0.000000001")

# Alpaca reports each execution as its own activity, so a partially filled order
# arrives as several of these rather than as one growing number.
CONFIRMED_FILL_TYPES = frozenset({"fill", "partial_fill"})

# Statuses that describe an order rather than an execution. None of them move a
# single share, and listing them by name is cheaper than rediscovering that.
NON_FILL_ORDER_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "pending_cancel",
        "pending_replace",
        "done_for_day",
        "canceled",
        "cancelled",
        "expired",
        "replaced",
        "rejected",
        "suspended",
        "stopped",
        "calculated",
        "held",
    }
)

APPLIED = "applied"
DUPLICATE = "duplicate"
UNATTRIBUTED = "unattributed"
REJECTED = "rejected"

FAULT_SELL_EXCEEDS_ATTRIBUTION = "SELL_FILL_EXCEEDS_STRATEGY_ATTRIBUTION"
FAULT_SYMBOL_MISMATCH = "FILL_SYMBOL_DOES_NOT_MATCH_ATTRIBUTION"
FAULT_ACCOUNT_MISMATCH = "FILL_ACCOUNT_DOES_NOT_MATCH_ATTRIBUTION"
FAULT_NOT_A_CONFIRMED_FILL = "EVENT_IS_NOT_A_CONFIRMED_FILL"


class OwnershipLifecycleError(ValueError):
    """A fill that cannot be reasoned about at all."""


@dataclass(frozen=True, slots=True)
class ConfirmedFill:
    """One execution the broker has confirmed happened.

    ``fill_id`` is Alpaca's ``broker_activity_id``: durable, unique per account,
    and stable across replays. It is the idempotency key, which is why it is
    required rather than derived -- a key we computed ourselves would change
    whenever our derivation changed, and every past fill would apply again.
    """

    fill_id: str
    broker_account_id: int
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    transaction_at: datetime
    activity_type: str = "fill"

    def __post_init__(self) -> None:
        if not self.fill_id:
            raise OwnershipLifecycleError(
                "a confirmed fill must carry the broker's activity id; without a "
                "durable key, replaying broker activity applies every fill twice"
            )
        if self.side not in ("buy", "sell"):
            raise OwnershipLifecycleError(f"unknown fill side {self.side!r}")
        if self.quantity <= 0:
            raise OwnershipLifecycleError("a confirmed fill moves a positive quantity")

    @property
    def is_confirmed_fill(self) -> bool:
        return self.activity_type.lower() in CONFIRMED_FILL_TYPES

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side == "buy" else -self.quantity

    @property
    def idempotency_key(self) -> tuple[int, str]:
        return (self.broker_account_id, self.fill_id)


@dataclass(frozen=True, slots=True)
class OrderAttribution:
    """Whose order this was.

    Written when the order is planned, and deliberately carries no quantity.
    Attribution says an order belongs to MOM_12_1; it says nothing about how
    much of it the market filled, and the moment it did, submitted size would
    become ownership evidence.
    """

    client_order_id: str
    strategy: str
    strategy_version: str
    broker_account_id: int
    symbol: str


@dataclass(frozen=True, slots=True)
class AttributedPosition:
    """One strategy's attributed holding, with the cost basis it was built at."""

    strategy: str
    strategy_version: str
    broker_account_id: int
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal | None
    as_of: datetime
    reconciliation_run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "broker_account_id": self.broker_account_id,
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "average_entry_price": (
                str(self.average_entry_price)
                if self.average_entry_price is not None
                else None
            ),
            "as_of": self.as_of.isoformat(),
            "reconciliation_run_id": self.reconciliation_run_id,
        }


@dataclass(frozen=True, slots=True)
class FillApplication:
    """What one fill did to the ledger, including when it did nothing."""

    status: str
    fill_id: str
    symbol: str
    strategy: str | None
    quantity_delta: Decimal
    resulting_quantity: Decimal | None
    reason: str | None = None

    @property
    def changed_ownership(self) -> bool:
        return self.status == APPLIED and self.quantity_delta != 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fill_id": self.fill_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "quantity_delta": str(self.quantity_delta),
            "resulting_quantity": (
                str(self.resulting_quantity)
                if self.resulting_quantity is not None
                else None
            ),
            "reason": self.reason,
        }


@dataclass(slots=True)
class OwnershipState:
    """The attributed book for one strategy on one account, plus what it has seen.

    ``applied_fill_ids`` is the whole of the idempotency guarantee. It is part of
    the state rather than a caller's bookkeeping because the two must be written
    together: a process that credits a fill and then fails before recording that
    it did will credit it again on restart.
    """

    strategy: str
    strategy_version: str
    broker_account_id: int
    positions: dict[str, AttributedPosition] = field(default_factory=dict)
    applied_fill_ids: set[str] = field(default_factory=set)
    faults: list[dict[str, Any]] = field(default_factory=list)

    def owned_quantity(self, symbol: str) -> Decimal:
        held = self.positions.get(symbol.upper())
        return held.quantity if held else Decimal(0)

    @property
    def has_faults(self) -> bool:
        return bool(self.faults)

    def to_ledger(self, *, source: str = "strategy_owned_positions") -> StrategyOwnershipLedger:
        """The read-side view the bridge and adapter consume.

        A faulted state yields an *unavailable* ledger. A book we know to be
        wrong is not a book to sell from, and the unavailable path already
        blocks every mutation, so the fault needs no second mechanism.
        """
        if self.has_faults:
            return StrategyOwnershipLedger.unavailable(self.strategy, source="faulted")
        return StrategyOwnershipLedger(
            strategy=self.strategy,
            positions={
                symbol: StrategyOwnedPosition(
                    strategy=self.strategy,
                    symbol=symbol,
                    quantity=held.quantity,
                    as_of=held.as_of,
                )
                for symbol, held in self.positions.items()
            },
            available=True,
            source=source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "broker_account_id": self.broker_account_id,
            "positions": [p.as_dict() for p in self.positions.values()],
            "applied_fill_count": len(self.applied_fill_ids),
            "faults": list(self.faults),
        }


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_PRECISION, rounding=ROUND_DOWN)


def _fault(state: OwnershipState, fill: ConfirmedFill, reason: str) -> FillApplication:
    state.faults.append(
        {"fill_id": fill.fill_id, "symbol": fill.symbol, "reason": reason}
    )
    return FillApplication(
        status=REJECTED,
        fill_id=fill.fill_id,
        symbol=fill.symbol,
        strategy=state.strategy,
        quantity_delta=Decimal(0),
        resulting_quantity=state.owned_quantity(fill.symbol),
        reason=reason,
    )


def apply_confirmed_fill(
    fill: ConfirmedFill,
    *,
    attribution: OrderAttribution | None,
    state: OwnershipState,
) -> FillApplication:
    """Move attribution by exactly the quantity the broker confirmed.

    Returns a result rather than raising for the cases that are data problems
    rather than programming errors, because one bad fill must not abort the
    ingestion of the rest -- but every such case records a fault, and a faulted
    state yields an unavailable ledger, which blocks.
    """
    symbol = fill.symbol.upper()

    if not fill.is_confirmed_fill:
        return _fault(state, fill, FAULT_NOT_A_CONFIRMED_FILL)

    # Idempotency first, before any other judgement: a replayed fill must be a
    # no-op even if it would otherwise be rejected, so that replaying broker
    # activity cannot manufacture new faults either.
    if fill.fill_id in state.applied_fill_ids:
        return FillApplication(
            status=DUPLICATE,
            fill_id=fill.fill_id,
            symbol=symbol,
            strategy=state.strategy,
            quantity_delta=Decimal(0),
            resulting_quantity=state.owned_quantity(symbol),
            reason="fill already applied",
        )

    if attribution is None or attribution.strategy != state.strategy:
        # Someone else's order, or an order nobody claimed. Both are ordinary in
        # a shared account, and neither is this strategy's business.
        return FillApplication(
            status=UNATTRIBUTED,
            fill_id=fill.fill_id,
            symbol=symbol,
            strategy=attribution.strategy if attribution else None,
            quantity_delta=Decimal(0),
            resulting_quantity=state.owned_quantity(symbol),
            reason="fill is not attributed to this strategy",
        )

    if attribution.symbol.upper() != symbol:
        return _fault(state, fill, FAULT_SYMBOL_MISMATCH)
    if attribution.broker_account_id != fill.broker_account_id:
        return _fault(state, fill, FAULT_ACCOUNT_MISMATCH)
    if fill.broker_account_id != state.broker_account_id:
        return _fault(state, fill, FAULT_ACCOUNT_MISMATCH)

    held = state.positions.get(symbol)
    current = held.quantity if held else Decimal(0)
    delta = _quantize(fill.signed_quantity)
    updated = current + delta

    if updated < 0:
        # This strategy is being told it sold more than it ever owned. Clamping
        # to zero would silently absorb the difference, and the difference is
        # someone else's shares.
        return _fault(state, fill, FAULT_SELL_EXCEEDS_ATTRIBUTION)

    if fill.side == "buy":
        prior_cost = (current * held.average_entry_price) if held and held.average_entry_price else Decimal(0)
        average = (
            ((prior_cost + fill.quantity * fill.price) / updated).quantize(
                PRICE_PRECISION, rounding=ROUND_DOWN
            )
            if updated > 0
            else None
        )
    else:
        # A sale realises part of the basis; it does not change what the
        # remaining shares cost.
        average = held.average_entry_price if held else None

    state.positions[symbol] = AttributedPosition(
        strategy=state.strategy,
        strategy_version=attribution.strategy_version or state.strategy_version,
        broker_account_id=state.broker_account_id,
        symbol=symbol,
        quantity=updated,
        average_entry_price=average if updated > 0 else None,
        # The last time this attribution was established, which is when the
        # market confirmed it -- not when we happened to ingest it.
        as_of=max(held.as_of, fill.transaction_at) if held else fill.transaction_at,
        reconciliation_run_id=held.reconciliation_run_id if held else None,
    )
    state.applied_fill_ids.add(fill.fill_id)
    return FillApplication(
        status=APPLIED,
        fill_id=fill.fill_id,
        symbol=symbol,
        strategy=state.strategy,
        quantity_delta=delta,
        resulting_quantity=updated,
    )


def apply_confirmed_fills(
    fills: list[ConfirmedFill],
    *,
    attributions: dict[str, OrderAttribution],
    state: OwnershipState,
) -> list[FillApplication]:
    """Apply many fills in a deterministic order.

    Sorted by execution time and then by fill id, so the ledger does not depend
    on the order the broker happened to return activities in. Without this,
    "replay produces the same ledger" would hold only when the pages came back
    the same way twice.
    """
    ordered = sorted(fills, key=lambda f: (f.transaction_at, f.fill_id))
    return [
        apply_confirmed_fill(
            fill, attribution=attributions.get(fill.client_order_id), state=state
        )
        for fill in ordered
    ]


def replay_fills(
    fills: list[ConfirmedFill],
    *,
    attributions: dict[str, OrderAttribution],
    strategy: str,
    strategy_version: str,
    broker_account_id: int,
) -> OwnershipState:
    """Rebuild the whole attributed book from the confirmed-fill history.

    This is the recovery path, and it is also the specification: whatever the
    incremental path produces, replaying every fill from the beginning must
    produce exactly the same thing.
    """
    state = OwnershipState(
        strategy=strategy,
        strategy_version=strategy_version,
        broker_account_id=broker_account_id,
    )
    apply_confirmed_fills(fills, attributions=attributions, state=state)
    return state


def ownership_change_for_order_status(status: str) -> Decimal:
    """Always zero. An order status is not a share.

    Kept as a named function so the rule is greppable and testable rather than
    implicit in the absence of code.
    """
    if status.lower() in CONFIRMED_FILL_TYPES:
        raise OwnershipLifecycleError(
            "a fill is not an order status; attribute it through "
            "apply_confirmed_fill so it is bounded by the idempotency key"
        )
    return Decimal(0)


def fills_from_rows(rows: list[dict[str, Any]]) -> list[ConfirmedFill]:
    """Build fills from ``broker_fills`` rows.

    ``broker_activity_id`` is carried through as the idempotency key because the
    table already guarantees it is unique per account.
    """
    return [
        ConfirmedFill(
            fill_id=str(row["broker_activity_id"]),
            broker_account_id=int(row["broker_account_id"]),
            broker_order_id=str(row["broker_order_id"]),
            client_order_id=str(row.get("client_order_id") or ""),
            symbol=str(row["symbol"]).upper(),
            side=str(row["side"]).lower(),
            quantity=Decimal(str(row["quantity"])),
            price=Decimal(str(row["price"])),
            transaction_at=row["transaction_at"],
            activity_type=str(row.get("activity_type") or "fill").lower(),
        )
        for row in rows
    ]


def attributions_from_rows(rows: list[dict[str, Any]]) -> dict[str, OrderAttribution]:
    """Build the attribution map from ``strategy_order_attributions`` rows."""
    return {
        str(row["client_order_id"]): OrderAttribution(
            client_order_id=str(row["client_order_id"]),
            strategy=str(row["strategy"]),
            strategy_version=str(row["strategy_version"]),
            broker_account_id=int(row["broker_account_id"]),
            symbol=str(row["symbol"]).upper(),
        )
        for row in rows
    }


def ownership_rows(state: OwnershipState) -> list[dict[str, Any]]:
    """The state as ``strategy_owned_positions`` rows, ready to upsert."""
    return [
        {
            "strategy": held.strategy,
            "strategy_version": held.strategy_version,
            "broker_account_id": held.broker_account_id,
            "symbol": symbol,
            "quantity": held.quantity,
            "average_entry_price": held.average_entry_price,
            "as_of": held.as_of,
            "reconciliation_run_id": held.reconciliation_run_id,
            "source": "confirmed_fill",
        }
        for symbol, held in sorted(state.positions.items())
    ]


def attributions_for_plan(
    plan: Any, *, broker_account_id: int
) -> list[dict[str, Any]]:
    """Attribution rows for every order a rebalance plan would send.

    This is what closes the loop: the plan names the orders, the attribution
    records whose they are, and later the fills say how much of them happened.
    Only the first two are known at plan time, which is exactly why no quantity
    appears here.
    """
    rows: list[dict[str, Any]] = []
    for symbol_plan in (*plan.symbol_plans, *plan.exits):
        payload = symbol_plan.order_payload
        if not payload or not symbol_plan.client_order_id:
            continue
        rows.append(
            {
                "broker_account_id": broker_account_id,
                "client_order_id": symbol_plan.client_order_id,
                "strategy": plan.signal.strategy,
                "strategy_version": plan.signal.strategy_version,
                "symbol": symbol_plan.symbol,
                "intended_side": payload["side"],
            }
        )
    return rows


def with_reconciliation_provenance(
    state: OwnershipState, *, run_id: int, symbols: list[str] | None = None
) -> OwnershipState:
    """Record which reconciliation run last agreed with the attribution.

    Provenance only. It does not alter a quantity, because a reconciliation run
    agreeing that the account holds what we think it holds is not itself a fill.
    """
    targets = set(symbols) if symbols is not None else set(state.positions)
    for symbol in targets & set(state.positions):
        state.positions[symbol] = replace(
            state.positions[symbol], reconciliation_run_id=run_id
        )
    return state
