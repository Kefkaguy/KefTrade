"""Position-reducing sells, without ever enabling short selling.

A rebalance strategy has to be able to leave a name, so the buy-only adapter is
not sufficient forever. But "allow sells" and "allow shorts" are one keystroke
apart, and the difference is the entire risk profile of the account.

So a sell here is not a side. It is a *reduction of a specific confirmed long
position*, and it is expressed that way throughout: every sell names the
position it reduces, carries that position's confirmed quantity, and is refused
if the arithmetic could take the holding through zero. There is no code path
that produces a sell without a matching long position, which means there is no
code path that opens a short.

Four independent gates, any one of which refuses:

* the position must exist and be **long** (quantity > 0);
* the reduction must not exceed the confirmed quantity;
* the position snapshot must be **fresh** -- a stale reconciliation means we do
  not actually know what we hold, and selling against a remembered position is
  how an account ends up short;
* both execution flags must still be on, exactly as for buys.

Nothing here submits an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.services.fractional_execution import (
    FRACTIONAL_ORDER_TYPE,
    FRACTIONAL_TIME_IN_FORCE,
    NOTIONAL_PRECISION,
    QTY_PRECISION,
    FractionalExecutionError,
    validate_order_payload,
)
from app.services.strategy_ownership import (
    ReconciliationEvidence,
    require_clean_reconciliation,
)


def max_position_staleness() -> timedelta:
    """How stale a stored snapshot may be before a reduction refuses.

    Read at call time rather than frozen at import, so the deployed value is the
    one that applies. The broker worker synchronizes roughly once per minute, so
    the 180-second default is a few sync cycles -- loose enough not to fight
    normal jitter, tight enough that a worker which has stopped is noticed.

    This is an outer bound, not the authority. Staleness cannot detect a
    position that changed *since* a perfectly fresh read, which is why every
    reduction is revalidated against the broker at the mutation boundary.
    """
    from app.settings import settings

    return timedelta(
        seconds=int(settings.broker_position_snapshot_max_staleness_seconds)
    )


# Kept as a module constant for callers that want the default without importing
# settings. The function above is what the guards actually consult.
DEFAULT_POSITION_STALENESS_SECONDS = 180

BLOCKER_NO_POSITION = "NO_CONFIRMED_LONG_POSITION"
BLOCKER_NOT_LONG = "POSITION_IS_NOT_LONG"
BLOCKER_EXCEEDS_POSITION = "SELL_EXCEEDS_LONG_POSITION"
BLOCKER_STALE_POSITION = "STALE_POSITION_SNAPSHOT"
BLOCKER_STALE_RECONCILIATION = "RECONCILIATION_NOT_CLEAN"

SELL_BLOCKERS: tuple[str, ...] = (
    BLOCKER_NO_POSITION,
    BLOCKER_NOT_LONG,
    BLOCKER_EXCEEDS_POSITION,
    BLOCKER_STALE_POSITION,
    BLOCKER_STALE_RECONCILIATION,
)


class ShortSellProhibited(FractionalExecutionError):
    """Raised when an order could take a holding at or through zero."""


@dataclass(frozen=True, slots=True)
class ConfirmedPosition:
    """A long position as the broker last confirmed it.

    ``quantity`` is what Alpaca says we hold, not what we believe we ordered.
    The distinction is the point: an intended fill is not a position.
    """

    symbol: str
    quantity: Decimal
    market_value: Decimal
    observed_at: datetime
    # Informational only. Reconciliation authority is ReconciliationEvidence,
    # which carries a run id and a timestamp; a string on a position could be
    # set to "clean" by whoever constructed it.
    reconciliation_status: str = "unknown"

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if self.observed_at.tzinfo is None:
            raise FractionalExecutionError(
                f"position snapshot for {self.symbol} carries no timezone; "
                "freshness cannot be established"
            )
        return (moment - self.observed_at) <= max_position_staleness()

    @property
    def price(self) -> Decimal | None:
        """Implied price per share, or None when the position is empty."""
        if self.quantity <= 0:
            return None
        return self.market_value / self.quantity


@dataclass(frozen=True, slots=True)
class ReductionOrder:
    """A sell that reduces one confirmed long position, and can do nothing else."""

    symbol: str
    position_quantity: Decimal
    sell_qty: Decimal
    closes_position: bool
    resulting_quantity: Decimal

    def payload(self, *, client_order_id: str) -> dict[str, Any]:
        body = {
            "symbol": self.symbol,
            "side": "sell",
            "type": FRACTIONAL_ORDER_TYPE,
            "time_in_force": FRACTIONAL_TIME_IN_FORCE,
            "qty": format(self.sell_qty.normalize(), "f"),
            "client_order_id": client_order_id,
        }
        return validate_order_payload(body)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": "sell",
            "position_quantity": str(self.position_quantity),
            "sell_qty": format(self.sell_qty.normalize(), "f"),
            "resulting_quantity": str(self.resulting_quantity),
            "closes_position": self.closes_position,
        }


def plan_position_reduction(
    *,
    position: ConfirmedPosition | None,
    target_dollars: Decimal,
    reference_price: Decimal,
    reconciliation: ReconciliationEvidence | None,
    now: datetime | None = None,
) -> ReductionOrder:
    """Reduce a long position toward ``target_dollars``, or refuse.

    ``target_dollars`` is where the position should end up, not how much to
    sell. Expressing it as a destination is what makes overshoot impossible to
    write: the sell quantity is derived from the gap and then clamped to the
    confirmed holding, so the resulting quantity is non-negative by
    construction.

    Refuses rather than clamping silently when the request is incoherent -- a
    sell larger than the position is a bug somewhere upstream, and quietly
    trimming it would hide that.
    """
    if position is None:
        raise ShortSellProhibited(
            f"no confirmed long position to reduce; {BLOCKER_NO_POSITION}"
        )
    if not position.is_long:
        raise ShortSellProhibited(
            f"{position.symbol} holds {position.quantity}, which is not a long "
            f"position; {BLOCKER_NOT_LONG}"
        )
    if not position.is_fresh(now=now):
        raise ShortSellProhibited(
            f"position snapshot for {position.symbol} is older than "
            f"{max_position_staleness()}; selling against a remembered position "
            f"is how an account ends up short; {BLOCKER_STALE_POSITION}"
        )
    require_clean_reconciliation(reconciliation)
    if reference_price <= 0:
        raise FractionalExecutionError(
            f"reference price for {position.symbol} must be positive"
        )
    if target_dollars < 0:
        raise ShortSellProhibited(
            f"a negative target of {target_dollars} for {position.symbol} would "
            "require a short position"
        )

    target_qty = (target_dollars / reference_price).quantize(
        QTY_PRECISION, rounding=ROUND_DOWN
    )
    if target_qty >= position.quantity:
        raise FractionalExecutionError(
            f"{position.symbol} target quantity {target_qty} is not below the "
            f"held {position.quantity}; this is not a reduction"
        )

    sell_qty = (position.quantity - target_qty).quantize(
        QTY_PRECISION, rounding=ROUND_DOWN
    )
    return _build_reduction(position, sell_qty)


def plan_full_exit(
    *,
    position: ConfirmedPosition | None,
    reconciliation: ReconciliationEvidence | None,
    now: datetime | None = None,
) -> ReductionOrder:
    """Close a long position exactly, selling the confirmed quantity and no more."""
    if position is None:
        raise ShortSellProhibited(
            f"no confirmed long position to close; {BLOCKER_NO_POSITION}"
        )
    if not position.is_long:
        raise ShortSellProhibited(
            f"{position.symbol} holds {position.quantity}; {BLOCKER_NOT_LONG}"
        )
    if not position.is_fresh(now=now):
        raise ShortSellProhibited(
            f"position snapshot for {position.symbol} is stale; "
            f"{BLOCKER_STALE_POSITION}"
        )
    require_clean_reconciliation(reconciliation)
    return _build_reduction(position, position.quantity)


def _build_reduction(position: ConfirmedPosition, sell_qty: Decimal) -> ReductionOrder:
    """The single place a ReductionOrder is constructed, and the last gate.

    Every path into a sell passes through here, so the "cannot cross zero"
    invariant is enforced once rather than restated at each caller.
    """
    if sell_qty <= 0:
        raise FractionalExecutionError(
            f"a reduction of {sell_qty} for {position.symbol} is not a sell"
        )
    if sell_qty > position.quantity:
        raise ShortSellProhibited(
            f"selling {sell_qty} of {position.symbol} against a confirmed "
            f"{position.quantity} would open a short of "
            f"{sell_qty - position.quantity}; {BLOCKER_EXCEEDS_POSITION}"
        )
    resulting = position.quantity - sell_qty
    if resulting < 0:  # unreachable given the check above; kept as a hard stop
        raise ShortSellProhibited(
            f"resulting quantity {resulting} for {position.symbol} is negative"
        )
    return ReductionOrder(
        symbol=position.symbol,
        position_quantity=position.quantity,
        sell_qty=sell_qty,
        closes_position=resulting == 0,
        resulting_quantity=resulting,
    )


def assert_sell_is_position_reducing(
    payload: dict[str, Any], positions: dict[str, ConfirmedPosition]
) -> dict[str, Any]:
    """Last-mile guard for any sell payload reaching the adapter.

    The adapter cannot see the plan that produced an order, so this re-derives
    the invariant from the payload and the confirmed book. A sell that survived
    every earlier gate but names a symbol we do not hold is still refused here.

    Notional sells are refused outright: a dollar amount cannot be checked
    against a share count without a price, and pricing it locally would mean
    guessing the fill. Reductions are expressed in shares.
    """
    if str(payload.get("side") or "").lower() != "sell":
        return payload
    symbol = str(payload.get("symbol") or "")
    if payload.get("notional") is not None:
        raise ShortSellProhibited(
            "a position-reducing sell must be expressed in shares, not notional: "
            "a dollar amount cannot be bounded by a share count without guessing "
            "the fill price"
        )
    position = positions.get(symbol)
    if position is None:
        raise ShortSellProhibited(
            f"no confirmed long position for {symbol}; {BLOCKER_NO_POSITION}"
        )
    if not position.is_long:
        raise ShortSellProhibited(f"{symbol} is not held long; {BLOCKER_NOT_LONG}")
    quantity = Decimal(str(payload.get("qty")))
    if quantity > position.quantity:
        raise ShortSellProhibited(
            f"selling {quantity} of {symbol} against a confirmed "
            f"{position.quantity} would open a short; {BLOCKER_EXCEEDS_POSITION}"
        )
    return payload


def reduction_dollars(order: ReductionOrder, reference_price: Decimal) -> Decimal:
    """Dollar value of a reduction, for the plan's diagnostics."""
    return (order.sell_qty * reference_price).quantize(
        NOTIONAL_PRECISION, rounding=ROUND_DOWN
    )


# ---------------------------------------------------------------------------
# The mutation boundary
# ---------------------------------------------------------------------------
#
# Staleness bounds how old a stored snapshot may be. They cannot bound what
# happened *since* it was taken -- a snapshot read one second ago is already
# history by the time an order reaches the venue, and in between a fill, a
# corporate action, or another process can have moved the position.
#
# So the last thing before any reduction leaves the process is a fresh read of
# the broker's own positions, compared against what we planned. Every
# disagreement refuses. Nothing is ever clamped down to fit: an oversized sell
# means the plan was built on state that no longer exists, and the correct
# response is to recompute the rebalance from the new broker state, not to
# quietly submit a smaller order the strategy never asked for.

BLOCKER_FRESH_READ_FAILED = "FRESH_POSITION_READ_FAILED"
BLOCKER_POSITION_DISAPPEARED = "POSITION_DISAPPEARED_BEFORE_SUBMIT"
BLOCKER_POSITION_SHRANK = "POSITION_SHRANK_BEFORE_SUBMIT"
BLOCKER_ENVIRONMENT = "NOT_ALPACA_PAPER"
BLOCKER_FLAGS_DISABLED = "EXECUTION_FLAGS_DISABLED"


class StalePositionAtSubmit(ShortSellProhibited):
    """The broker disagrees with the plan. Recompute; do not shrink the order."""


def parse_broker_positions(
    payload: Any, *, observed_at: datetime, reconciliation_status: str
) -> dict[str, ConfirmedPosition]:
    """Normalise a fresh ``/v2/positions`` body into confirmed long positions.

    Short positions are parsed but kept negative rather than discarded, so a
    caller asking to reduce one is refused loudly instead of being told the
    symbol is simply absent.
    """
    positions: dict[str, ConfirmedPosition] = {}
    for row in payload or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        quantity = Decimal(str(row.get("qty") or "0"))
        market_value = Decimal(str(row.get("market_value") or "0"))
        positions[symbol] = ConfirmedPosition(
            symbol=symbol,
            quantity=quantity,
            market_value=market_value,
            observed_at=observed_at,
            reconciliation_status=reconciliation_status,
        )
    return positions


def revalidate_reduction_against_fresh_positions(
    *,
    symbol: str,
    requested_qty: Decimal,
    fresh_positions: dict[str, ConfirmedPosition],
    stored: ConfirmedPosition | None,
    reconciliation: ReconciliationEvidence | None,
) -> ConfirmedPosition:
    """The final gate. Refuses unless the broker still supports the reduction.

    ``stored`` is the snapshot the plan was built from. Comparing against it
    catches the case that staleness cannot: a position that was real, is still
    real, and is now *smaller* than the order assumes.
    """
    try:
        require_clean_reconciliation(reconciliation)
    except Exception as refusal:
        raise StalePositionAtSubmit(
            f"reconciliation evidence does not support a mutation: {refusal}"
        ) from refusal

    fresh = fresh_positions.get(symbol)
    if fresh is None:
        raise StalePositionAtSubmit(
            f"{symbol} is no longer a broker position; the plan was built "
            f"against state that no longer exists; {BLOCKER_POSITION_DISAPPEARED}"
        )
    if fresh.quantity <= 0:
        raise StalePositionAtSubmit(
            f"{symbol} now holds {fresh.quantity}, which is not a long "
            f"position; {BLOCKER_NOT_LONG}"
        )
    if requested_qty > fresh.quantity:
        raise StalePositionAtSubmit(
            f"selling {requested_qty} of {symbol} against a freshly confirmed "
            f"{fresh.quantity} would open a short. The order is rejected rather "
            f"than reduced: recompute the rebalance from the new broker state; "
            f"{BLOCKER_EXCEEDS_POSITION}"
        )
    if stored is not None and fresh.quantity < stored.quantity:
        raise StalePositionAtSubmit(
            f"{symbol} shrank from {stored.quantity} to {fresh.quantity} between "
            f"planning and submission; the plan is stale even though this "
            f"particular order would still fit; {BLOCKER_POSITION_SHRANK}"
        )
    return fresh
