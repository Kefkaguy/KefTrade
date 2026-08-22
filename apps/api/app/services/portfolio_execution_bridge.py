"""Portfolio execution bridge for frozen portfolio signals.

The existing external-paper path is one symbol per deployment, sized from a
stop-loss distance, and its ``proposed_broker_orders`` row requires a positive
``stop_price``. MOM_12_1 has none of those things: it is a monthly equal-weight
book of a few hundred names with no per-name stop. Forcing it into that model
would have meant inventing stops, which is inventing risk parameters the
research never had.

So this is a separate bridge. It consumes an **immutable signal CSV** and emits
an observe-only rebalance plan: what we hold, what the strategy says we should
hold, the dollar delta per name, and the exact Alpaca payload that would express
it. It never submits anything, and it shares the existing safety controls rather
than restating them -- the fractional engine validates payloads, the sell module
guarantees reductions cannot cross zero.

The signal is read, never recomputed. Selection, weighting, lookbacks, rebalance
timing and eligibility all belong to the frozen strategy; this module's only
opinion is how to express the resulting weights as orders a broker will accept.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from app.services.fractional_execution import (
    BLOCKER_INSUFFICIENT_BUYING_POWER,
    BLOCKER_MISSING_PRICE,
    BLOCKER_NONFRACTIONABLE,
    BLOCKER_NOT_TRADABLE,
    BLOCKER_UNKNOWN_FRACTIONABILITY,
    FRACTIONAL_ORDER_TYPE,
    FRACTIONAL_TIME_IN_FORCE,
    NOTIONAL_PRECISION,
    SHARE_POLICY_NOTIONAL,
    AssetFact,
    FractionalExecutionError,
    deterministic_client_order_id,
    preflight_assets,
    validate_order_payload,
)
from app.services.position_reducing_sell import (
    ConfirmedPosition,
    ReductionOrder,
    ShortSellProhibited,
    plan_full_exit,
    plan_position_reduction,
)
from app.services.strategy_ownership import (
    OwnershipUnavailable,
    ReconciliationEvidence,
    ReconciliationEvidenceMissing,
    StrategyOwnershipLedger,
    assert_within_strategy_ownership,
    require_clean_reconciliation,
    require_ownership_ledger,
    sellable_quantity,
)

STRATEGY_MOM_12_1 = "MOM_12_1"
MOM_12_1_UNIVERSE_HASH = "f7b50c2b0c0882df"

# Equal-dollar weighting is the research definition, so notional market DAY
# orders express it exactly: every name receives the same dollars regardless of
# its price. Fractional qty would reintroduce a per-name rounding residue that
# scales with price, for no benefit.
MOM_12_1_SHARE_POLICY = SHARE_POLICY_NOTIONAL

PROVENANCE_FORWARD = "forward"
PROVENANCE_TEST_REPLAY = "test_replay"
PROVENANCES: tuple[str, ...] = (PROVENANCE_FORWARD, PROVENANCE_TEST_REPLAY)

BLOCKER_UNIVERSE_HASH = "UNIVERSE_HASH_MISMATCH"
BLOCKER_WEIGHT_MISMATCH = "SIGNAL_WEIGHTS_NOT_EQUAL"
BLOCKER_SELECTION_MISMATCH = "SELECTION_SET_MISMATCH"
BLOCKER_SELL_UNSAFE = "SELL_WOULD_CROSS_ZERO"
BLOCKER_RECONCILIATION = "RECONCILIATION_NOT_CLEAN"
BLOCKER_STRATEGY_VERSION = "SIGNAL_STRATEGY_VERSION_INVALID"
BLOCKER_OWNERSHIP = "STRATEGY_OWNERSHIP_UNAVAILABLE"


# ---------------------------------------------------------------------------
# The immutable signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioSignal:
    """One frozen portfolio signal, exactly as the strategy emitted it."""

    strategy: str
    strategy_version: str
    universe_hash: str
    signal_date: date
    intended_execution_date: date
    symbols: tuple[str, ...]
    provenance: str
    source_path: str
    source_sha256: str

    @property
    def selected_count(self) -> int:
        return len(self.symbols)

    @property
    def target_weight(self) -> Decimal:
        """Equal weight across ALL selected names, per the frozen definition."""
        return Decimal(1) / Decimal(self.selected_count)

    @property
    def is_forward_evidence(self) -> bool:
        return self.provenance == PROVENANCE_FORWARD

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "universe_hash": self.universe_hash,
            "signal_date": self.signal_date.isoformat(),
            "intended_execution_date": self.intended_execution_date.isoformat(),
            "selected_count": self.selected_count,
            "target_weight": str(self.target_weight),
            "provenance": self.provenance,
            "is_forward_evidence": self.is_forward_evidence,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }


def load_portfolio_signal(
    path: Path,
    *,
    provenance: str,
    expected_universe_hash: str = MOM_12_1_UNIVERSE_HASH,
    strategy: str = STRATEGY_MOM_12_1,
) -> PortfolioSignal:
    """Read a signal CSV. Reads, never recomputes.

    ``provenance`` must be stated by the caller rather than inferred from the
    file, because a historical CSV replayed for plumbing validation is
    indistinguishable on disk from a genuine forward signal. Marking it is the
    caller's assertion, and a replay can never be promoted to forward evidence
    by accident.

    The file is hashed so a plan can name the exact bytes it was built from.
    """
    if provenance not in PROVENANCES:
        raise FractionalExecutionError(
            f"provenance must be one of {list(PROVENANCES)}, got {provenance!r}"
        )
    if not path.is_file():
        raise FractionalExecutionError(f"signal CSV is missing at {path}")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise FractionalExecutionError(f"signal CSV at {path} holds no rows")

    required = {"symbol", "signal_date", "intended_execution_date", "universe_hash"}
    missing_columns = required - set(rows[0])
    if missing_columns:
        raise FractionalExecutionError(
            f"signal CSV at {path} is missing columns {sorted(missing_columns)}"
        )

    symbols = tuple(str(row["symbol"]).strip().upper() for row in rows)
    if len(set(symbols)) != len(symbols):
        duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
        raise FractionalExecutionError(
            f"signal CSV names {duplicates} more than once; equal weighting is "
            "undefined when a symbol appears twice"
        )

    signal_dates = {row["signal_date"] for row in rows}
    execution_dates = {row["intended_execution_date"] for row in rows}
    hashes = {row["universe_hash"] for row in rows}
    for label, values in (
        ("signal_date", signal_dates),
        ("intended_execution_date", execution_dates),
        ("universe_hash", hashes),
    ):
        if len(values) != 1:
            raise FractionalExecutionError(
                f"signal CSV at {path} carries {len(values)} distinct {label} "
                f"values {sorted(values)}; one signal is one rebalance"
            )

    universe_hash = hashes.pop()
    if universe_hash != expected_universe_hash:
        raise FractionalExecutionError(
            f"signal CSV universe hash {universe_hash!r} does not match the "
            f"frozen {expected_universe_hash!r}; {BLOCKER_UNIVERSE_HASH}"
        )

    # Exactly one non-empty version. A file mixing versions is two signals in
    # one, and taking the minimum -- as this once did -- would silently pick a
    # winner rather than refuse.
    versions = {str(row.get("strategy_version") or "").strip() for row in rows}
    if len(versions) != 1:
        raise FractionalExecutionError(
            f"signal CSV at {path} carries {len(versions)} distinct "
            f"strategy_version values {sorted(versions)}; one signal is one "
            f"strategy version; {BLOCKER_STRATEGY_VERSION}"
        )
    strategy_version = versions.pop()
    if not strategy_version:
        raise FractionalExecutionError(
            f"signal CSV at {path} carries an empty strategy_version; "
            f"{BLOCKER_STRATEGY_VERSION}"
        )
    if provenance == PROVENANCE_FORWARD and strategy_version.lower() == "unknown":
        raise FractionalExecutionError(
            "a forward signal may not carry strategy_version 'unknown': forward "
            "evidence has to name the version that produced it; "
            f"{BLOCKER_STRATEGY_VERSION}"
        )
    return PortfolioSignal(
        strategy=strategy,
        strategy_version=strategy_version,
        universe_hash=universe_hash,
        signal_date=date.fromisoformat(signal_dates.pop()),
        intended_execution_date=date.fromisoformat(execution_dates.pop()),
        symbols=symbols,
        provenance=provenance,
        source_path=str(path),
        source_sha256=digest,
    )


# ---------------------------------------------------------------------------
# The rebalance plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolPlan:
    """One name's complete state: held, wanted, and the order that bridges them."""

    symbol: str
    target_weight: Decimal
    target_dollars: Decimal
    current_quantity: Decimal
    current_dollars: Decimal
    required_dollar_delta: Decimal
    action: str  # buy | reduce | exit | hold
    order_payload: dict[str, Any] | None
    client_order_id: str | None
    tradable: bool | None
    fractionable: bool | None
    blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "target_weight": str(self.target_weight),
            "target_dollars": str(self.target_dollars),
            "current_quantity": str(self.current_quantity),
            "current_dollars": str(self.current_dollars),
            "required_dollar_delta": str(self.required_dollar_delta),
            "action": self.action,
            "order_payload": self.order_payload,
            "client_order_id": self.client_order_id,
            "tradable": self.tradable,
            "fractionable": self.fractionable,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """A complete observe-only rebalance. Never an order by itself."""

    signal: PortfolioSignal
    share_policy: str
    allocated_capital: Decimal
    target_dollars_per_name: Decimal
    symbol_plans: tuple[SymbolPlan, ...]
    exits: tuple[SymbolPlan, ...]
    blocked: bool
    blockers: tuple[str, ...]
    blocked_symbols: tuple[str, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    observe_only: bool = True

    @property
    def total_target_notional(self) -> Decimal:
        return sum((p.target_dollars for p in self.symbol_plans), Decimal(0))

    @property
    def residual_cash(self) -> Decimal:
        return self.allocated_capital - self.total_target_notional

    def as_dict(self) -> dict[str, Any]:
        return {
            "observe_only": self.observe_only,
            "orders_submitted": False,
            "signal": self.signal.as_dict(),
            "share_policy": self.share_policy,
            "allocated_capital": str(self.allocated_capital),
            "target_dollars_per_name": str(self.target_dollars_per_name),
            "total_target_notional": str(self.total_target_notional),
            "residual_cash": str(self.residual_cash),
            "blocked": self.blocked,
            "blockers": list(self.blockers),
            "blocked_symbols": list(self.blocked_symbols),
            "symbol_plans": [p.as_dict() for p in self.symbol_plans],
            "exits": [p.as_dict() for p in self.exits],
            "diagnostics": self.diagnostics,
        }


def build_rebalance_plan(
    *,
    signal: PortfolioSignal,
    allocated_capital: Decimal,
    reference_prices: dict[str, Decimal],
    asset_facts: dict[str, AssetFact],
    positions: dict[str, ConfirmedPosition] | None = None,
    ownership: StrategyOwnershipLedger | None = None,
    reconciliation: ReconciliationEvidence | None = None,
    buying_power: Decimal | None = None,
    share_policy: str = MOM_12_1_SHARE_POLICY,
) -> RebalancePlan:
    """Turn a frozen signal into an observe-only rebalance plan.

    Buys are notional so every name receives identical dollars, which is what
    equal-dollar weighting means. Exits are share-quantity reductions, because a
    dollar amount cannot be bounded by a share count without guessing the fill
    price -- see ``position_reducing_sell``.

    A single non-fractionable or unknown-fractionability name blocks the whole
    rebalance. The alternatives are all worse: dropping it changes the selection
    set, redistributing its weight changes the weighting, and substituting
    changes both. Each would silently edit a frozen strategy.
    """
    held = positions or {}
    symbols = list(signal.symbols)
    target = (allocated_capital / Decimal(signal.selected_count)).quantize(
        NOTIONAL_PRECISION, rounding=ROUND_DOWN
    )

    blockers: list[str] = []
    blocked_symbols: set[str] = set()

    # Reconciliation is evidence about a specific run, never a default string.
    try:
        require_clean_reconciliation(reconciliation)
        reconciliation_ok = True
    except ReconciliationEvidenceMissing:
        blockers.append(BLOCKER_RECONCILIATION)
        reconciliation_ok = False

    # Ownership is what makes an exit legitimate. Without a ledger the account
    # book is the only thing left to read, and reading it would attribute every
    # holding in the account to this strategy.
    try:
        ledger = require_ownership_ledger(ownership, strategy=signal.strategy)
        ownership_ok = True
    except OwnershipUnavailable:
        ledger = StrategyOwnershipLedger.unavailable(
            signal.strategy, source="unavailable"
        )
        blockers.append(BLOCKER_OWNERSHIP)
        ownership_ok = False

    preflight = preflight_assets(symbols, asset_facts, share_policy=share_policy)
    if preflight["blocked"]:
        blockers.extend(preflight["blockers"])
        blocked_symbols.update(preflight["blocked_symbols"])

    missing_price = sorted(s for s in symbols if reference_prices.get(s) is None)
    if missing_price:
        blockers.append(BLOCKER_MISSING_PRICE)
        blocked_symbols.update(missing_price)

    rebalance_key = signal.intended_execution_date.isoformat()
    symbol_plans: list[SymbolPlan] = []

    for symbol in symbols:
        fact = asset_facts.get(symbol)
        position = held.get(symbol)
        current_qty = position.quantity if position else Decimal(0)
        current_dollars = position.market_value if position else Decimal(0)
        delta = target - current_dollars

        payload: dict[str, Any] | None = None
        client_order_id: str | None = None
        action = "hold"
        symbol_blockers: list[str] = []

        if symbol in blocked_symbols:
            symbol_blockers.append(
                BLOCKER_NONFRACTIONABLE
                if symbol in preflight["nonfractionable_symbols"]
                else BLOCKER_UNKNOWN_FRACTIONABILITY
                if symbol in preflight["unknown_fractionability_symbols"]
                else BLOCKER_MISSING_PRICE
                if symbol in missing_price
                else BLOCKER_NOT_TRADABLE
            )

        if not symbol_blockers and delta > 0:
            action = "buy"
            client_order_id = deterministic_client_order_id(
                strategy_name=signal.strategy,
                strategy_version=signal.strategy_version,
                rebalance_key=rebalance_key,
                symbol=symbol,
            )
            payload = validate_order_payload({
                "symbol": symbol,
                "side": "buy",
                "type": FRACTIONAL_ORDER_TYPE,
                "time_in_force": FRACTIONAL_TIME_IN_FORCE,
                "notional": format(delta.quantize(NOTIONAL_PRECISION, ROUND_DOWN), "f"),
                "client_order_id": client_order_id,
            })
        elif not symbol_blockers and delta < 0 and position is not None:
            # Held more than the target: reduce, never below zero.
            action = "reduce"
            try:
                # Bound by ownership before availability: selling more than we
                # own takes another strategy's position.
                assert_within_strategy_ownership(
                    strategy=signal.strategy,
                    symbol=symbol,
                    requested_qty=position.quantity - (target / reference_prices[symbol]),
                    ledger=ledger,
                )
                reduction = plan_position_reduction(
                    position=position,
                    target_dollars=target,
                    reference_price=reference_prices[symbol],
                    reconciliation=reconciliation,
                )
                client_order_id = deterministic_client_order_id(
                    strategy_name=signal.strategy,
                    strategy_version=signal.strategy_version,
                    rebalance_key=f"{rebalance_key}:reduce",
                    symbol=symbol,
                )
                payload = reduction.payload(client_order_id=client_order_id)
            except (ShortSellProhibited, OwnershipUnavailable, ReconciliationEvidenceMissing):
                symbol_blockers.append(BLOCKER_SELL_UNSAFE)
                blockers.append(BLOCKER_SELL_UNSAFE)
                blocked_symbols.add(symbol)
                payload = None
                action = "blocked"
            except FractionalExecutionError:
                # Not a reduction after rounding: the position already sits at
                # target within a share fraction.
                action = "hold"

        symbol_plans.append(
            SymbolPlan(
                symbol=symbol,
                target_weight=signal.target_weight,
                target_dollars=target,
                current_quantity=current_qty,
                current_dollars=current_dollars,
                required_dollar_delta=delta,
                action=action,
                order_payload=payload,
                client_order_id=client_order_id,
                tradable=fact.tradable if fact else None,
                fractionable=fact.fractionable if fact else None,
                blockers=tuple(symbol_blockers),
            )
        )

    exits = _plan_exits(
        signal, held, ledger, reconciliation, rebalance_key, blockers,
        blocked_symbols, ownership_ok=ownership_ok,
    )

    total_target = target * Decimal(signal.selected_count)
    if buying_power is not None:
        required = sum(
            (p.required_dollar_delta for p in symbol_plans if p.action == "buy"),
            Decimal(0),
        )
        if required > buying_power:
            blockers.append(BLOCKER_INSUFFICIENT_BUYING_POWER)

    diagnostics = {
        "requested_portfolio_capital": str(allocated_capital),
        "selected_count": signal.selected_count,
        "target_weight": str(signal.target_weight),
        "target_dollars_per_name": str(target),
        "total_target_notional": str(total_target),
        "estimated_residual_cash": str(allocated_capital - total_target),
        "fractionable_count": preflight["fractionable_count"],
        "nonfractionable_count": preflight["nonfractionable_count"],
        "unknown_fractionability_count": preflight["unknown_fractionability_count"],
        "max_absolute_weight_error": "0",
        "max_relative_weight_error": "0",
        "buy_count": sum(1 for p in symbol_plans if p.action == "buy"),
        "reduce_count": sum(1 for p in symbol_plans if p.action == "reduce"),
        "hold_count": sum(1 for p in symbol_plans if p.action == "hold"),
        "exit_count": len(exits),
        "reconciliation": reconciliation.as_dict() if reconciliation else None,
        "reconciliation_evidence_present": reconciliation_ok,
        "ownership_ledger": ledger.as_dict(),
        "ownership_available": ownership_ok,
        "strategy_owned_symbols": list(ledger.held_symbols),
        "account_positions_seen": sorted(held),
        "account_positions_are_not_ownership": True,
        "preflight": preflight,
    }

    return RebalancePlan(
        signal=signal,
        share_policy=share_policy,
        allocated_capital=allocated_capital,
        target_dollars_per_name=target,
        symbol_plans=tuple(symbol_plans),
        exits=tuple(exits),
        blocked=bool(blockers),
        blockers=tuple(dict.fromkeys(blockers)),
        blocked_symbols=tuple(sorted(blocked_symbols)),
        diagnostics=diagnostics,
    )


def _plan_exits(
    signal: PortfolioSignal,
    held: dict[str, ConfirmedPosition],
    ledger: StrategyOwnershipLedger,
    reconciliation: ReconciliationEvidence | None,
    rebalance_key: str,
    blockers: list[str],
    blocked_symbols: set[str],
    *,
    ownership_ok: bool,
) -> list[SymbolPlan]:
    """Names **this strategy owns** that the new signal does not select.

    Driven by the ownership ledger, never by the account position book. The
    account may hold anything -- another strategy's book, a manual position, a
    legacy holding -- and none of it becomes MOM's to liquidate merely because
    MOM stopped selecting the symbol.

    Where the strategy owns less than the account holds, the exit sells the
    owned portion only. The remainder belongs to whoever put it there.
    """
    if not ownership_ok:
        # No ledger, no exits. Falling back to `held` here is exactly the bug
        # this function exists to avoid.
        return []

    selected = set(signal.symbols)
    exits: list[SymbolPlan] = []
    for symbol in ledger.held_symbols:
        if symbol in selected:
            continue
        owned = ledger.owned_quantity(symbol)
        position = held.get(symbol)
        broker_qty = position.quantity if position else Decimal(0)

        payload: dict[str, Any] | None = None
        client_order_id: str | None = None
        symbol_blockers: list[str] = []
        action = "exit"
        try:
            sellable = sellable_quantity(
                strategy=signal.strategy,
                symbol=symbol,
                ledger=ledger,
                broker_quantity=broker_qty,
            )
            if sellable <= 0:
                # Owned on paper, absent at the broker: a reconciliation
                # problem, not a sell.
                symbol_blockers.append(BLOCKER_SELL_UNSAFE)
                blockers.append(BLOCKER_SELL_UNSAFE)
                blocked_symbols.add(symbol)
                action = "blocked"
            else:
                attributed = ConfirmedPosition(
                    symbol=symbol,
                    quantity=sellable,
                    market_value=(
                        position.market_value * (sellable / broker_qty)
                        if position and broker_qty > 0
                        else Decimal(0)
                    ),
                    observed_at=position.observed_at if position else ledger_as_of(ledger, symbol),
                    reconciliation_status=reconciliation.status if reconciliation else "unknown",
                )
                reduction: ReductionOrder = plan_full_exit(
                    position=attributed, reconciliation=reconciliation
                )
                client_order_id = deterministic_client_order_id(
                    strategy_name=signal.strategy,
                    strategy_version=signal.strategy_version,
                    rebalance_key=f"{rebalance_key}:exit",
                    symbol=symbol,
                )
                payload = reduction.payload(client_order_id=client_order_id)
        except (ShortSellProhibited, OwnershipUnavailable, ReconciliationEvidenceMissing):
            symbol_blockers.append(BLOCKER_SELL_UNSAFE)
            blockers.append(BLOCKER_SELL_UNSAFE)
            blocked_symbols.add(symbol)
            action = "blocked"

        exits.append(
            SymbolPlan(
                symbol=symbol,
                target_weight=Decimal(0),
                target_dollars=Decimal(0),
                current_quantity=owned,
                current_dollars=position.market_value if position else Decimal(0),
                required_dollar_delta=-(position.market_value if position else Decimal(0)),
                action=action,
                order_payload=payload,
                client_order_id=client_order_id,
                tradable=None,
                fractionable=None,
                blockers=tuple(symbol_blockers),
            )
        )
    return exits


def ledger_as_of(ledger: StrategyOwnershipLedger, symbol: str):
    """When the ledger last attributed this symbol."""
    from datetime import UTC, datetime

    entry = ledger.positions.get(symbol)
    return entry.as_of if entry else datetime.now(UTC)


def verify_plan_against_signal(plan: RebalancePlan) -> dict[str, Any]:
    """Prove the plan reproduces the signal exactly, before anyone trusts it.

    Every claim here is checkable from the plan and the signal alone, which is
    what makes it a proof rather than a description.
    """
    signal_symbols = list(plan.signal.symbols)
    plan_symbols = [p.symbol for p in plan.symbol_plans]
    ideal_weight = plan.signal.target_weight

    weights_equal = all(p.target_weight == ideal_weight for p in plan.symbol_plans)
    dollars_equal = all(
        p.target_dollars == plan.target_dollars_per_name for p in plan.symbol_plans
    )
    residual = plan.residual_cash
    # Residual is entirely rounding when it is smaller than one cent per name.
    rounding_bound = Decimal("0.01") * Decimal(plan.signal.selected_count)

    return {
        "selection_matches_signal": plan_symbols == signal_symbols,
        "selected_count_matches": len(plan_symbols) == plan.signal.selected_count,
        "target_weight_is_one_over_n": weights_equal,
        "target_dollars_are_equal": dollars_equal,
        "total_within_allocated_capital": plan.total_target_notional
        <= plan.allocated_capital,
        "residual_cash": str(residual),
        "residual_within_rounding_bound": Decimal(0) <= residual < rounding_bound,
        "rounding_bound": str(rounding_bound),
        "max_absolute_weight_error": plan.diagnostics["max_absolute_weight_error"],
        "max_relative_weight_error": plan.diagnostics["max_relative_weight_error"],
        "symbols_omitted": sorted(set(signal_symbols) - set(plan_symbols)),
        "symbols_added": sorted(set(plan_symbols) - set(signal_symbols)),
        "orders_submitted": False,
        "observe_only": plan.observe_only,
        "provenance": plan.signal.provenance,
        "is_forward_evidence": plan.signal.is_forward_evidence,
    }
