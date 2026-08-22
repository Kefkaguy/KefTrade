"""Strategy-owned positions, and the reconciliation evidence a sell requires.

Two facts a broker account cannot tell you, both of which a rebalance needs.

**Who owns a position.** Alpaca reports one book per account. If MOM_12_1 shares
that account with anything else -- another strategy, a manual holding, a legacy
position -- then "AAPL is held" is not the same claim as "MOM_12_1 holds AAPL",
and an exit built from the account book would liquidate someone else's position
because MOM stopped selecting the symbol. So ownership is an explicit ledger,
and a symbol's presence in the account is never evidence of it.

**Whether reconciliation is clean.** That is a fact about a specific
reconciliation run at a specific time, not a string a caller can default to. It
is carried as evidence with an id and a timestamp, so "clean" always answers
*which run, and how long ago*.

Both fail closed. Absent ownership blocks the rebalance; absent evidence blocks
the sell.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

BLOCKER_NO_OWNERSHIP_LEDGER = "STRATEGY_OWNERSHIP_LEDGER_UNAVAILABLE"
BLOCKER_NOT_OWNED = "SYMBOL_NOT_OWNED_BY_STRATEGY"
BLOCKER_EXCEEDS_OWNED = "SELL_EXCEEDS_STRATEGY_OWNED_QUANTITY"
BLOCKER_NO_RECONCILIATION_EVIDENCE = "RECONCILIATION_EVIDENCE_MISSING"
BLOCKER_RECONCILIATION_NOT_CLEAN = "RECONCILIATION_NOT_CLEAN"
BLOCKER_RECONCILIATION_STALE = "RECONCILIATION_EVIDENCE_STALE"

OWNERSHIP_BLOCKERS: tuple[str, ...] = (
    BLOCKER_NO_OWNERSHIP_LEDGER,
    BLOCKER_NOT_OWNED,
    BLOCKER_EXCEEDS_OWNED,
)


class OwnershipUnavailable(ValueError):
    """The ledger cannot say what this strategy owns, so nothing may be sold."""


class ReconciliationEvidenceMissing(ValueError):
    """A sell was attempted without evidence that reconciliation is clean."""


# ---------------------------------------------------------------------------
# Reconciliation evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    """A specific reconciliation run, its verdict, and when it ran.

    Constructed from a real row rather than a literal, so a caller cannot assert
    cleanliness by typing the word. ``run_id`` is what makes the claim auditable
    after the fact.
    """

    run_id: int
    status: str
    completed_at: datetime
    broker_account_id: int | None = None

    @property
    def is_clean(self) -> bool:
        return self.status == "clean"

    def age(self, *, now: datetime | None = None) -> timedelta:
        moment = now or datetime.now(UTC)
        if self.completed_at.tzinfo is None:
            raise ReconciliationEvidenceMissing(
                f"reconciliation run {self.run_id} carries no timezone; its age "
                "cannot be established"
            )
        return moment - self.completed_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "completed_at": self.completed_at.isoformat(),
            "broker_account_id": self.broker_account_id,
            "is_clean": self.is_clean,
        }


def require_clean_reconciliation(
    evidence: ReconciliationEvidence | None,
    *,
    max_age: timedelta | None = None,
    now: datetime | None = None,
) -> ReconciliationEvidence:
    """Refuse unless a real, recent, clean reconciliation run says so.

    ``None`` is a refusal, not a default. The previous shape of this check took
    a string defaulting to ``"clean"``, which meant every caller that forgot the
    argument asserted cleanliness by omission -- the exact fail-open this
    replaces.
    """
    if evidence is None:
        raise ReconciliationEvidenceMissing(
            "a reduction sell requires evidence that reconciliation is clean; "
            "no evidence was supplied, and absence is not cleanliness; "
            f"{BLOCKER_NO_RECONCILIATION_EVIDENCE}"
        )
    if not evidence.is_clean:
        raise ReconciliationEvidenceMissing(
            f"reconciliation run {evidence.run_id} is {evidence.status!r}, not "
            f"clean; {BLOCKER_RECONCILIATION_NOT_CLEAN}"
        )
    if max_age is not None and evidence.age(now=now) > max_age:
        raise ReconciliationEvidenceMissing(
            f"reconciliation run {evidence.run_id} completed "
            f"{evidence.age(now=now)} ago, beyond the {max_age} bound; "
            f"{BLOCKER_RECONCILIATION_STALE}"
        )
    return evidence


# ---------------------------------------------------------------------------
# Strategy ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyOwnedPosition:
    """How much of a symbol one strategy is accountable for."""

    strategy: str
    symbol: str
    quantity: Decimal
    as_of: datetime

    @property
    def is_held(self) -> bool:
        return self.quantity > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "as_of": self.as_of.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class StrategyOwnershipLedger:
    """What one strategy owns, and whether that is knowable at all.

    ``available=False`` is a distinct state from "owns nothing". An empty ledger
    is a claim; an unavailable one is the absence of a claim, and only the first
    can support an exit.
    """

    strategy: str
    positions: dict[str, StrategyOwnedPosition]
    available: bool
    source: str

    def owned_quantity(self, symbol: str) -> Decimal:
        held = self.positions.get(symbol)
        return held.quantity if held else Decimal(0)

    def owns(self, symbol: str) -> bool:
        return self.owned_quantity(symbol) > 0

    @property
    def held_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(s for s, p in self.positions.items() if p.is_held))

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "available": self.available,
            "source": self.source,
            "held_symbols": list(self.held_symbols),
            "positions": [p.as_dict() for p in self.positions.values()],
        }

    @classmethod
    def unavailable(cls, strategy: str, *, source: str) -> StrategyOwnershipLedger:
        return cls(strategy=strategy, positions={}, available=False, source=source)


def require_ownership_ledger(
    ledger: StrategyOwnershipLedger | None, *, strategy: str
) -> StrategyOwnershipLedger:
    """Refuse to plan any exit without a ledger that can answer.

    Falling back to the account book here would be the whole bug: it would make
    every position the account holds look like this strategy's, and a rebalance
    would then liquidate holdings it never opened.
    """
    if ledger is None or not ledger.available:
        raise OwnershipUnavailable(
            f"no strategy ownership ledger for {strategy}; the account position "
            "book is not ownership evidence, so no exit can be planned; "
            f"{BLOCKER_NO_OWNERSHIP_LEDGER}"
        )
    if ledger.strategy != strategy:
        raise OwnershipUnavailable(
            f"ownership ledger names {ledger.strategy!r}, not {strategy!r}"
        )
    return ledger


def sellable_quantity(
    *,
    strategy: str,
    symbol: str,
    ledger: StrategyOwnershipLedger,
    broker_quantity: Decimal,
) -> Decimal:
    """The most this strategy may sell: the lesser of owned and available.

    The two bounds answer different questions and neither substitutes for the
    other. Ownership says how much we are *entitled* to sell; the broker's
    confirmed long says how much *exists*. Selling more than we own takes
    someone else's position; selling more than exists opens a short.
    """
    owned = ledger.owned_quantity(symbol)
    if owned <= 0:
        raise OwnershipUnavailable(
            f"{strategy} does not own {symbol}; a position the account happens "
            f"to hold is not this strategy's to sell; {BLOCKER_NOT_OWNED}"
        )
    if broker_quantity <= 0:
        return Decimal(0)
    return min(owned, broker_quantity)


def assert_within_strategy_ownership(
    *,
    strategy: str,
    symbol: str,
    requested_qty: Decimal,
    ledger: StrategyOwnershipLedger,
) -> Decimal:
    """Refuse a sell larger than this strategy's attributed quantity."""
    owned = ledger.owned_quantity(symbol)
    if owned <= 0:
        raise OwnershipUnavailable(
            f"{strategy} does not own {symbol}; {BLOCKER_NOT_OWNED}"
        )
    if requested_qty > owned:
        raise OwnershipUnavailable(
            f"{strategy} attempted to sell {requested_qty} of {symbol} while "
            f"owning {owned}; the excess belongs to another strategy or to the "
            f"account, and is not this rebalance's to liquidate; "
            f"{BLOCKER_EXCEEDS_OWNED}"
        )
    return owned


def ledger_from_rows(
    rows: list[dict[str, Any]], *, strategy: str, source: str = "database"
) -> StrategyOwnershipLedger:
    """Build a ledger from ``strategy_owned_positions`` rows."""
    positions: dict[str, StrategyOwnedPosition] = {}
    for row in rows:
        symbol = str(row["symbol"]).upper()
        positions[symbol] = StrategyOwnedPosition(
            strategy=strategy,
            symbol=symbol,
            quantity=Decimal(str(row["quantity"])),
            as_of=row["as_of"],
        )
    return StrategyOwnershipLedger(
        strategy=strategy, positions=positions, available=True, source=source
    )
