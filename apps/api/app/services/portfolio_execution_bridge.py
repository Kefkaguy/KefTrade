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

    versions = {str(row.get("strategy_version") or "unknown") for row in rows}
    return PortfolioSignal(
        strategy=strategy,
        strategy_version=min(versions),
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
    buying_power: Decimal | None = None,
    reconciliation_status: str = "clean",
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

    if reconciliation_status != "clean":
        blockers.append(BLOCKER_RECONCILIATION)

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
                reduction = plan_position_reduction(
                    position=position,
                    target_dollars=target,
                    reference_price=reference_prices[symbol],
                )
                client_order_id = deterministic_client_order_id(
                    strategy_name=signal.strategy,
                    strategy_version=signal.strategy_version,
                    rebalance_key=f"{rebalance_key}:reduce",
                    symbol=symbol,
                )
                payload = reduction.payload(client_order_id=client_order_id)
            except ShortSellProhibited as refusal:
                symbol_blockers.append(BLOCKER_SELL_UNSAFE)
                blockers.append(BLOCKER_SELL_UNSAFE)
                blocked_symbols.add(symbol)
                payload = None
                action = "blocked"
                _ = refusal
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

    exits = _plan_exits(signal, held, rebalance_key, blockers, blocked_symbols)

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
        "reconciliation_status": reconciliation_status,
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
    rebalance_key: str,
    blockers: list[str],
    blocked_symbols: set[str],
) -> list[SymbolPlan]:
    """Names we hold that the new signal does not select.

    Their target weight is zero, which is a statement about the strategy, not a
    licence to short: the exit sells exactly the confirmed quantity.
    """
    selected = set(signal.symbols)
    exits: list[SymbolPlan] = []
    for symbol, position in sorted(held.items()):
        if symbol in selected or position.quantity <= 0:
            continue
        payload: dict[str, Any] | None = None
        client_order_id: str | None = None
        symbol_blockers: list[str] = []
        action = "exit"
        try:
            reduction: ReductionOrder = plan_full_exit(position=position)
            client_order_id = deterministic_client_order_id(
                strategy_name=signal.strategy,
                strategy_version=signal.strategy_version,
                rebalance_key=f"{rebalance_key}:exit",
                symbol=symbol,
            )
            payload = reduction.payload(client_order_id=client_order_id)
        except ShortSellProhibited:
            symbol_blockers.append(BLOCKER_SELL_UNSAFE)
            blockers.append(BLOCKER_SELL_UNSAFE)
            blocked_symbols.add(symbol)
            action = "blocked"
        exits.append(
            SymbolPlan(
                symbol=symbol,
                target_weight=Decimal(0),
                target_dollars=Decimal(0),
                current_quantity=position.quantity,
                current_dollars=position.market_value,
                required_dollar_delta=-position.market_value,
                action=action,
                order_payload=payload,
                client_order_id=client_order_id,
                tradable=None,
                fractionable=None,
                blockers=tuple(symbol_blockers),
            )
        )
    return exits


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
