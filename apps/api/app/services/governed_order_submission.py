"""The only sanctioned path from a planned order to ``POST /v2/orders``.

Attribution has to exist, durably, before an order reaches the venue. If the
order fills and no row says whose it was, the fill is unattributable forever:
the ownership lifecycle joins fills to strategies through the client order id,
and there is no later evidence that can reconstruct an attribution nobody wrote.
The account would hold shares belonging to a strategy that cannot claim them,
and the next rebalance would size against a position it does not know it has.

Until now that ordering was a convention -- a caller was expected to record
attributions before submitting. This module makes it structural. Verification
mints a ``SubmissionCapability`` naming that one order, and the portfolio
adapter's mutating entry point requires one; its public ``submit_order`` refuses
outright. So the adapter is not merely the wrong door for an unattributed order,
it is a door with nothing behind it.

**Why a service rather than a check inside the adapter.** The adapter is a
broker client: it speaks HTTP and knows nothing about our tables. Giving it a
connection would put database access inside ``AlpacaPaperBrokerAdapter``, whose
frozen buy-only contract is what existing deployments approved -- and a
capability added to the base class is inherited by every subclass whether or not
its release was reviewed. So the gate sits immediately above the adapter, where
it can hold both halves without either one growing into the other.

Ordering, and the reason for it:

1. **Persist** the attribution and commit it. Committed before the POST, because
   an attribution with no order is inert -- no fill will ever reference it --
   while an order with no attribution is the unrecoverable case.
2. **Verify** by reading the row back and comparing all six fields against the
   order. A row that exists is not evidence until it is evidence *about this
   order*.
3. **Broker safety checks**, unchanged, inside the adapter: execution flags,
   ownership bounds, reconciliation freshness, and for a sell the fresh
   ``GET /v2/positions`` re-read at the mutation boundary.
4. **POST**.

Any failure in 1 or 2 raises before step 3, so no request of any kind is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.brokers.submission_capability import SubmissionCapability
from app.services.strategy_ownership_repository import (
    OwnershipPersistenceError,
    read_order_attribution,
    record_order_attribution,
)

ATTRIBUTION_MISSING = "ORDER_ATTRIBUTION_NOT_DURABLE"
ATTRIBUTION_CONFLICT = "ORDER_ATTRIBUTION_CONFLICTS_WITH_ORDER"

# The fields that must agree between the stored row and the order in hand.
# Quantity is deliberately absent: attribution says whose order this is, never
# how large it was, so that submitted size can never be mistaken for ownership.
ATTRIBUTED_FIELDS = (
    "broker_account_id",
    "client_order_id",
    "strategy",
    "strategy_version",
    "symbol",
    "intended_side",
)


class OrderAttributionError(RuntimeError):
    """Attribution could not be established, so no order may be sent."""


class AttributionConflict(OrderAttributionError):
    """A row exists for this client order id, describing a different order."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Whose order this is, and which order that is. No quantity, by design."""

    broker_account_id: int
    client_order_id: str
    strategy: str
    strategy_version: str
    symbol: str
    intended_side: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker_account_id": self.broker_account_id,
            "client_order_id": self.client_order_id,
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "intended_side": self.intended_side,
        }


def intent_from_payload(
    payload: dict[str, Any],
    *,
    broker_account_id: int,
    strategy: str,
    strategy_version: str,
) -> OrderIntent:
    """Derive the intent from the order actually being sent.

    Read off the payload rather than accepted alongside it, so the attribution
    cannot describe a different symbol or side from the one on the wire.
    """
    client_order_id = str(payload.get("client_order_id") or "")
    if not client_order_id:
        raise OrderAttributionError(
            "a governed order must carry a deterministic client order id; "
            "without one it cannot be attributed, and an unattributable fill "
            f"can never be credited to a strategy; {ATTRIBUTION_MISSING}"
        )
    symbol = str(payload.get("symbol") or "").upper()
    side = str(payload.get("side") or "").lower()
    if not symbol or side not in ("buy", "sell"):
        raise OrderAttributionError(
            f"cannot attribute an order with symbol {symbol!r} and side {side!r}"
        )
    if not strategy or not strategy_version:
        raise OrderAttributionError(
            "attribution requires both a strategy and its version; a version of "
            "'unknown' would make two different strategy revisions "
            "indistinguishable in the ownership ledger"
        )
    return OrderIntent(
        broker_account_id=broker_account_id,
        client_order_id=client_order_id,
        strategy=strategy,
        strategy_version=strategy_version,
        symbol=symbol,
        intended_side=side,
    )


def assert_attribution_matches(stored: dict[str, Any] | None, intent: OrderIntent) -> None:
    """Refuse unless the stored row describes exactly this order.

    A deterministic client order id is derived from strategy, version, rebalance
    key and symbol, so a stored row that disagrees on any of those means the id
    was reused for something else -- a collision, a hand-edited row, or a bug in
    the derivation. Accepting it would attribute this order's fills to whatever
    the old row named.
    """
    if stored is None:
        raise OrderAttributionError(
            f"no durable attribution for client order id "
            f"{intent.client_order_id!r}; {ATTRIBUTION_MISSING}"
        )
    expected = intent.as_dict()
    differences = {
        field: {"stored": stored.get(field), "order": expected[field]}
        for field in ATTRIBUTED_FIELDS
        if _normalise(stored.get(field)) != _normalise(expected[field])
    }
    if differences:
        raise AttributionConflict(
            f"the stored attribution for {intent.client_order_id!r} describes a "
            f"different order ({differences}); {ATTRIBUTION_CONFLICT}"
        )


def _normalise(value: Any) -> Any:
    """Coerce a numeric id written as text, and compare everything else exactly.

    Only the account id is loosened, because some drivers hand an integer column
    back as a string. Symbol and side are already normalised at both the write
    and the intent, so any remaining difference is a real one -- and client order
    ids are case-sensitive at the venue, so folding their case here would let two
    genuinely different orders compare equal.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value


def persist_and_verify_attribution(
    conn: Any,
    intent: OrderIntent,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Write the attribution, commit it, and read it back. Fails closed.

    The read-back is not ceremony. It is what turns "we issued an INSERT" into
    "the row is there and says what we meant", which are different claims when a
    conflicting row already existed -- the insert is a silent no-op in that case,
    and only the read notices.
    """
    try:
        written = record_order_attribution(
            conn,
            broker_account_id=intent.broker_account_id,
            client_order_id=intent.client_order_id,
            strategy=intent.strategy,
            strategy_version=intent.strategy_version,
            symbol=intent.symbol,
            intended_side=intent.intended_side,
            now=now,
        )
        if commit:
            # Durable before the POST. An attribution with no order is inert;
            # an order with no attribution cannot be repaired afterwards.
            conn.commit()
    except (OwnershipPersistenceError, OrderAttributionError):
        raise
    except Exception as error:
        raise OrderAttributionError(
            f"attribution for {intent.client_order_id!r} could not be persisted "
            f"({error.__class__.__name__}: {error}); no order may be sent; "
            f"{ATTRIBUTION_MISSING}"
        ) from error

    try:
        stored = read_order_attribution(
            conn,
            broker_account_id=intent.broker_account_id,
            client_order_id=intent.client_order_id,
        )
    except Exception as error:
        raise OrderAttributionError(
            f"attribution for {intent.client_order_id!r} could not be verified "
            f"({error.__class__.__name__}: {error}); an unverified attribution "
            f"is not a durable one; {ATTRIBUTION_MISSING}"
        ) from error

    assert_attribution_matches(stored, intent)
    return {
        "attribution": intent.as_dict(),
        "newly_written": written,
        "verified": True,
        "quantity_persisted": False,
    }


class GovernedOrderSubmitter:
    """Holds the connection and the adapter so neither has to hold the other.

    The adapter stays a pure broker client and the repository stays pure
    persistence; this is the only place that knows an order needs both.
    """

    def __init__(self, *, conn: Any, adapter: Any, broker_account_id: int) -> None:
        self._conn = conn
        self._adapter = adapter
        self._broker_account_id = broker_account_id
        self.attributions: list[dict[str, Any]] = []

    async def submit(
        self,
        payload: dict[str, Any],
        *,
        strategy: str,
        strategy_version: str,
        confirmed_positions: dict[str, Any] | None = None,
        ownership_ledger: Any = None,
        reconciliation: Any = None,
        now: datetime | None = None,
    ) -> Any:
        """Attribute durably, verify, then let the adapter run its own gates.

        The adapter's checks are not duplicated here. Flags, ownership bounds,
        reconciliation freshness and the fresh position re-read all still happen
        inside it, at the mutation boundary, where they belong -- this only
        guarantees they are never reached by an order nobody can account for.
        """
        intent = intent_from_payload(
            payload,
            broker_account_id=self._broker_account_id,
            strategy=strategy,
            strategy_version=strategy_version,
        )
        # Steps 1 and 2. Anything wrong here raises, and the adapter is never
        # touched, so no request of any kind reaches Alpaca.
        record = persist_and_verify_attribution(self._conn, intent, now=now)
        self.attributions.append(record)

        # The capability exists only on this side of verification. It is what
        # makes the ordering structural rather than remembered: the adapter's
        # mutating entry point has nothing to act on without one, so an order
        # nobody attributed cannot reach it even by calling it directly.
        capability = SubmissionCapability.after_verified_attribution(
            attribution=record["attribution"], verified=record["verified"]
        )

        # Step 3 and 4, inside the adapter: execution flags, ownership, the
        # fresh GET /v2/positions for a sell, then POST.
        wire = dict(payload)
        wire.setdefault("strategy", strategy)
        return await self._adapter._submit_governed_order(
            wire,
            capability=capability,
            confirmed_positions=confirmed_positions,
            ownership_ledger=ownership_ledger,
            reconciliation=reconciliation,
        )

    async def submit_plan_orders(
        self,
        plan: Any,
        *,
        confirmed_positions: dict[str, Any] | None = None,
        ownership_ledger: Any = None,
        reconciliation: Any = None,
    ) -> list[Any]:
        """Submit a whole rebalance, attributing each order before sending it.

        Stops at the first failure rather than continuing. A rebalance that
        submitted some of its names and abandoned the rest would leave the
        portfolio in a state neither the old signal nor the new one describes.
        """
        responses = []
        for symbol_plan in (*plan.symbol_plans, *plan.exits):
            if not symbol_plan.order_payload:
                continue
            responses.append(
                await self.submit(
                    symbol_plan.order_payload,
                    strategy=plan.signal.strategy,
                    strategy_version=plan.signal.strategy_version,
                    confirmed_positions=confirmed_positions,
                    ownership_ledger=ownership_ledger,
                    reconciliation=reconciliation,
                )
            )
        return responses
