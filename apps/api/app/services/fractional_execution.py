"""Governed fractional-share execution for Alpaca paper.

KefTrade's execution risk policy has always asserted ``whole_shares: True``. That
is correct for the single-name stop-loss strategies the external paper path was
built for, and wrong for an equal-weight book of a few hundred names in a
~$100k account, where a whole-share constraint quantises every position by up to
a full share price and the resulting weights stop resembling the strategy.

The fix is not to flip the flag. Whole shares stay the default everywhere, and
fractional execution becomes an explicit, versioned share policy that a
deployment must be configured into. An already-approved configuration keeps the
exact policy it was approved under, because ``persist_policy`` re-hashes the
policy dict and refuses any drift.

Three rules do the safety work here:

* ``qty`` and ``notional`` are mutually exclusive at Alpaca, so a payload
  carrying both is rejected locally before any HTTP call.
* Fractional orders are market/DAY only, so anything else is refused rather
  than sent and rejected remotely.
* A selected name that is not fractionable **blocks the whole rebalance**. It is
  never dropped, substituted, or absorbed by redistributing its capital -- each
  of those silently edits the strategy's selection set.

Nothing in this module submits an order or enables a flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

# Alpaca accepts fractional quantities to nine decimal places; asking for more
# precision than the venue keeps would make our local weight arithmetic disagree
# with the fill.
QTY_PRECISION = Decimal("0.000000001")
# Notional is dollars and cents.
NOTIONAL_PRECISION = Decimal("0.01")

# Alpaca supports fractional quantities and notional only for market orders with
# day time-in-force.
FRACTIONAL_ORDER_TYPE = "market"
FRACTIONAL_TIME_IN_FORCE = "day"

SHARE_POLICY_WHOLE = "whole_shares"
SHARE_POLICY_FRACTIONAL_QTY = "fractional_qty"
SHARE_POLICY_NOTIONAL = "notional"
SHARE_POLICIES: tuple[str, ...] = (
    SHARE_POLICY_WHOLE,
    SHARE_POLICY_FRACTIONAL_QTY,
    SHARE_POLICY_NOTIONAL,
)
FRACTIONAL_POLICIES: frozenset[str] = frozenset(
    {SHARE_POLICY_FRACTIONAL_QTY, SHARE_POLICY_NOTIONAL}
)

BLOCKER_NONFRACTIONABLE = "NONFRACTIONABLE_SELECTED_ASSET"
BLOCKER_UNKNOWN_FRACTIONABILITY = "UNKNOWN_FRACTIONABILITY"
BLOCKER_NOT_TRADABLE = "ASSET_NOT_TRADABLE"
BLOCKER_MISSING_PRICE = "MISSING_REFERENCE_PRICE"
BLOCKER_INSUFFICIENT_CAPITAL = "INSUFFICIENT_PORTFOLIO_CAPITAL"
BLOCKER_INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
BLOCKER_WEIGHT_ERROR = "WEIGHT_ERROR_EXCEEDS_TOLERANCE"


class FractionalExecutionError(ValueError):
    """A local refusal, raised before any broker call."""


# ---------------------------------------------------------------------------
# Share policy
# ---------------------------------------------------------------------------


def resolve_share_policy(risk_policy: dict[str, Any]) -> str:
    """Which share policy a risk-policy version authorises.

    Legacy policies carry only ``whole_shares``. They keep whole shares, which
    is the behaviour every already-approved deployment was approved under. A
    policy must name ``share_policy`` explicitly to get anything else -- there
    is deliberately no inference from ``whole_shares: False``, because an
    absent-or-false flag is not a statement about *which* fractional mode was
    intended.
    """
    declared = risk_policy.get("share_policy")
    if declared is None:
        if risk_policy.get("whole_shares", True):
            return SHARE_POLICY_WHOLE
        raise FractionalExecutionError(
            "risk policy disables whole_shares without naming a share_policy. "
            "Fractional execution is explicit: set share_policy to one of "
            f"{sorted(FRACTIONAL_POLICIES)}."
        )
    if declared not in SHARE_POLICIES:
        raise FractionalExecutionError(
            f"unknown share_policy {declared!r}; expected one of {list(SHARE_POLICIES)}"
        )
    if declared in FRACTIONAL_POLICIES and risk_policy.get("whole_shares", True):
        raise FractionalExecutionError(
            f"share_policy {declared!r} contradicts whole_shares=True in the same "
            "policy version; a policy must not assert both"
        )
    return declared


def requires_fractionable_assets(share_policy: str) -> bool:
    return share_policy in FRACTIONAL_POLICIES


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def validate_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Refuse a malformed order locally, before it reaches Alpaca.

    The mutual exclusivity of ``qty`` and ``notional`` is the important one:
    Alpaca rejects a payload carrying both, but a *locally* rejected payload
    never becomes an ambiguous submission whose outcome we then have to
    reconcile.
    """
    has_qty = payload.get("qty") is not None
    has_notional = payload.get("notional") is not None

    if has_qty and has_notional:
        raise FractionalExecutionError(
            "qty and notional are mutually exclusive; an order may carry one or "
            "the other, never both"
        )
    if not has_qty and not has_notional:
        raise FractionalExecutionError("an order must carry either qty or notional")

    quantity = Decimal(str(payload["qty"])) if has_qty else None
    notional = Decimal(str(payload["notional"])) if has_notional else None
    if quantity is not None and quantity <= 0:
        raise FractionalExecutionError(f"qty must be positive, got {quantity}")
    if notional is not None and notional <= 0:
        raise FractionalExecutionError(f"notional must be positive, got {notional}")

    fractional = bool(notional is not None or (quantity is not None and quantity % 1))
    if fractional:
        order_type = str(payload.get("type") or "").lower()
        time_in_force = str(payload.get("time_in_force") or "").lower()
        if order_type != FRACTIONAL_ORDER_TYPE or time_in_force != FRACTIONAL_TIME_IN_FORCE:
            raise FractionalExecutionError(
                "fractional quantity and notional orders are market/day only; got "
                f"type={order_type!r} time_in_force={time_in_force!r}"
            )
        # A bracket needs a whole-share child order to attach to, so Alpaca
        # rejects the combination. Refusing here keeps the reason legible.
        if payload.get("order_class") and str(payload["order_class"]).lower() != "simple":
            raise FractionalExecutionError(
                "fractional orders cannot carry an order_class such as bracket; "
                f"got {payload['order_class']!r}"
            )
    return payload


# ---------------------------------------------------------------------------
# Asset preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssetFact:
    """What we know about one symbol's tradability, from Alpaca."""

    symbol: str
    tradable: bool
    fractionable: bool | None  # None means never observed

    @property
    def fractionability_known(self) -> bool:
        return self.fractionable is not None


def preflight_assets(
    symbols: list[str], facts: dict[str, AssetFact], *, share_policy: str
) -> dict[str, Any]:
    """Check every selected name before any order is constructed.

    Fails closed on unknown fractionability. "We never asked Alpaca about this
    symbol" is not evidence that it can be traded fractionally, and treating it
    as such would be exactly the silent assumption this gate exists to remove.
    """
    missing = [s for s in symbols if s not in facts]
    not_tradable = [s for s in symbols if s in facts and not facts[s].tradable]
    unknown: list[str] = []
    nonfractionable: list[str] = []

    if requires_fractionable_assets(share_policy):
        for symbol in symbols:
            fact = facts.get(symbol)
            if fact is None or not fact.tradable:
                continue
            if not fact.fractionability_known:
                unknown.append(symbol)
            elif not fact.fractionable:
                nonfractionable.append(symbol)

    blockers: list[str] = []
    if missing or not_tradable:
        blockers.append(BLOCKER_NOT_TRADABLE)
    if unknown:
        blockers.append(BLOCKER_UNKNOWN_FRACTIONABILITY)
    if nonfractionable:
        blockers.append(BLOCKER_NONFRACTIONABLE)

    fractionable_count = sum(
        1 for s in symbols if (f := facts.get(s)) and f.fractionable is True
    )
    return {
        "share_policy": share_policy,
        "requires_fractionable": requires_fractionable_assets(share_policy),
        "selected_count": len(symbols),
        "fractionable_count": fractionable_count,
        "nonfractionable_count": len(nonfractionable),
        "unknown_fractionability_count": len(unknown),
        "blockers": blockers,
        "blocked": bool(blockers),
        # Surfaced by name, because "some symbol failed" is not actionable.
        "blocked_symbols": sorted({*missing, *not_tradable, *unknown, *nonfractionable}),
        "missing_symbols": sorted(missing),
        "not_tradable_symbols": sorted(not_tradable),
        "unknown_fractionability_symbols": sorted(unknown),
        "nonfractionable_symbols": sorted(nonfractionable),
    }


# ---------------------------------------------------------------------------
# Equal-weight portfolio sizing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SizedOrder:
    """One would-submit order. Exactly one of qty / notional is set."""

    symbol: str
    reference_price: Decimal
    target_dollars: Decimal
    qty: Decimal | None
    notional: Decimal | None
    planned_dollars: Decimal

    def payload(self, *, client_order_id: str) -> dict[str, Any]:
        """The exact Alpaca payload, validated before it is returned."""
        body: dict[str, Any] = {
            "symbol": self.symbol,
            "side": "buy",
            "type": FRACTIONAL_ORDER_TYPE,
            "time_in_force": FRACTIONAL_TIME_IN_FORCE,
            "client_order_id": client_order_id,
        }
        if self.qty is not None:
            body["qty"] = format(self.qty.normalize(), "f")
        else:
            body["notional"] = format(self.notional, "f")
        return validate_order_payload(body)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reference_price": str(self.reference_price),
            "target_dollars": str(self.target_dollars),
            "qty": format(self.qty.normalize(), "f") if self.qty is not None else None,
            "notional": format(self.notional, "f") if self.notional is not None else None,
            "planned_dollars": str(self.planned_dollars),
        }


@dataclass(frozen=True, slots=True)
class SizingPlan:
    """A complete would-submit order set, with its sizing diagnostics."""

    share_policy: str
    portfolio_capital: Decimal
    selected_count: int
    target_dollars_per_name: Decimal
    orders: tuple[SizedOrder, ...]
    blocked: bool
    blockers: tuple[str, ...]
    blocked_symbols: tuple[str, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def total_requested_notional(self) -> Decimal:
        return sum((o.planned_dollars for o in self.orders), Decimal(0))

    @property
    def estimated_residual_cash(self) -> Decimal:
        return self.portfolio_capital - self.total_requested_notional

    def as_dict(self) -> dict[str, Any]:
        return {
            "share_policy": self.share_policy,
            "portfolio_capital": str(self.portfolio_capital),
            "selected_count": self.selected_count,
            "target_dollars_per_name": str(self.target_dollars_per_name),
            "total_requested_notional": str(self.total_requested_notional),
            "estimated_residual_cash": str(self.estimated_residual_cash),
            "blocked": self.blocked,
            "blockers": list(self.blockers),
            "blocked_symbols": list(self.blocked_symbols),
            "orders": [o.as_dict() for o in self.orders],
            "diagnostics": self.diagnostics,
        }


def plan_equal_weight_portfolio(
    *,
    symbols: list[str],
    reference_prices: dict[str, Decimal],
    portfolio_capital: Decimal,
    share_policy: str,
    asset_facts: dict[str, AssetFact] | None = None,
    buying_power: Decimal | None = None,
    max_absolute_weight_error: Decimal | None = None,
) -> SizingPlan:
    """Size an equal-weight book of ``len(symbols)`` names into ``portfolio_capital``.

    The algorithm, exactly:

    1. ``target = portfolio_capital / N``, computed once, in Decimal.
    2. Per name, depending on the share policy:
       * ``notional`` -- request ``target`` dollars, rounded **down** to the
         cent. Rounding down is what keeps the sum inside the capital: rounding
         to nearest could put the book over budget by up to half a cent per
         name, which across 300 names is real money and a real reject.
       * ``fractional_qty`` -- ``qty = target / price``, rounded **down** to
         nine decimals.
       * ``whole_shares`` -- ``qty = floor(target / price)``. Preserved
         unchanged, including the fact that a name priced above ``target``
         sizes to zero.
    3. Planned dollars per name is the notional itself, or ``qty * price``.

    The selection set is never altered. Names are not dropped for being
    expensive, capital is never redistributed away from a blocked name, and no
    substitute is chosen -- all three would edit a frozen strategy's portfolio
    under the guise of execution.
    """
    if share_policy not in SHARE_POLICIES:
        raise FractionalExecutionError(f"unknown share_policy {share_policy!r}")
    if not symbols:
        raise FractionalExecutionError("an equal-weight portfolio needs at least one name")
    if portfolio_capital <= 0:
        raise FractionalExecutionError(
            f"portfolio capital must be positive, got {portfolio_capital}"
        )

    unique = list(dict.fromkeys(symbols))
    if len(unique) != len(symbols):
        raise FractionalExecutionError(
            "the selection set contains duplicate symbols; equal weighting is "
            "undefined when a name appears twice"
        )

    count = len(unique)
    target = (portfolio_capital / Decimal(count)).quantize(
        NOTIONAL_PRECISION, rounding=ROUND_DOWN
    )

    blockers: list[str] = []
    blocked_symbols: set[str] = set()

    facts = asset_facts or {}
    preflight = preflight_assets(unique, facts, share_policy=share_policy) if facts else None
    if preflight and preflight["blocked"]:
        blockers.extend(preflight["blockers"])
        blocked_symbols.update(preflight["blocked_symbols"])

    missing_price = sorted(s for s in unique if reference_prices.get(s) is None)
    if missing_price:
        blockers.append(BLOCKER_MISSING_PRICE)
        blocked_symbols.update(missing_price)

    orders: list[SizedOrder] = []
    for symbol in unique:
        price = reference_prices.get(symbol)
        if price is None or price <= 0:
            continue
        qty: Decimal | None = None
        notional: Decimal | None = None

        if share_policy == SHARE_POLICY_NOTIONAL:
            notional = target
            planned = notional
        elif share_policy == SHARE_POLICY_FRACTIONAL_QTY:
            qty = (target / price).quantize(QTY_PRECISION, rounding=ROUND_DOWN)
            planned = (qty * price).quantize(NOTIONAL_PRECISION, rounding=ROUND_DOWN)
        else:
            qty = (target / price).to_integral_value(rounding=ROUND_DOWN)
            planned = (qty * price).quantize(NOTIONAL_PRECISION, rounding=ROUND_DOWN)

        if (qty is not None and qty <= 0) or (notional is not None and notional <= 0):
            # A whole-share book legitimately sizes an expensive name to zero.
            # That is the legacy behaviour and is reported, not silently hidden.
            continue
        orders.append(
            SizedOrder(
                symbol=symbol,
                reference_price=price,
                target_dollars=target,
                qty=qty,
                notional=notional,
                planned_dollars=planned,
            )
        )

    total = sum((o.planned_dollars for o in orders), Decimal(0))
    if total > portfolio_capital:
        blockers.append(BLOCKER_INSUFFICIENT_CAPITAL)
    if buying_power is not None and total > buying_power:
        blockers.append(BLOCKER_INSUFFICIENT_BUYING_POWER)

    weight_errors = _weight_errors(orders, target=target, capital=portfolio_capital)
    if (
        max_absolute_weight_error is not None
        and weight_errors["max_absolute_weight_error"] > max_absolute_weight_error
    ):
        blockers.append(BLOCKER_WEIGHT_ERROR)

    diagnostics: dict[str, Any] = {
        "requested_portfolio_capital": str(portfolio_capital),
        "selected_count": count,
        "sized_count": len(orders),
        "unsized_count": count - len(orders),
        "target_dollars_per_name": str(target),
        "total_requested_notional": str(total),
        "estimated_residual_cash": str(portfolio_capital - total),
        "fractionable_count": preflight["fractionable_count"] if preflight else None,
        "nonfractionable_count": preflight["nonfractionable_count"] if preflight else None,
        "unknown_fractionability_count": (
            preflight["unknown_fractionability_count"] if preflight else None
        ),
        **{k: str(v) for k, v in weight_errors.items()},
        "buying_power": str(buying_power) if buying_power is not None else None,
        "preflight": preflight,
    }

    return SizingPlan(
        share_policy=share_policy,
        portfolio_capital=portfolio_capital,
        selected_count=count,
        target_dollars_per_name=target,
        orders=tuple(orders),
        blocked=bool(blockers),
        blockers=tuple(dict.fromkeys(blockers)),
        blocked_symbols=tuple(sorted(blocked_symbols)),
        diagnostics=diagnostics,
    )


def _weight_errors(
    orders: list[SizedOrder], *, target: Decimal, capital: Decimal
) -> dict[str, Decimal]:
    """How far the sized book departs from exact equal weighting.

    Absolute error is in portfolio-weight units against the ideal ``1/N``;
    relative error is the same departure expressed as a fraction of that ideal,
    which is the number that says whether the strategy still looks like itself.
    """
    if not orders or capital <= 0:
        return {
            "max_absolute_weight_error": Decimal(0),
            "max_relative_weight_error": Decimal(0),
        }
    ideal = Decimal(1) / Decimal(len(orders))
    max_absolute = Decimal(0)
    max_relative = Decimal(0)
    total = sum((o.planned_dollars for o in orders), Decimal(0))
    if total <= 0:
        return {
            "max_absolute_weight_error": Decimal(1),
            "max_relative_weight_error": Decimal(1),
        }
    for order in orders:
        weight = order.planned_dollars / total
        absolute = abs(weight - ideal)
        max_absolute = max(max_absolute, absolute)
        max_relative = max(max_relative, absolute / ideal)
    return {
        "max_absolute_weight_error": max_absolute,
        "max_relative_weight_error": max_relative,
    }


def deterministic_client_order_id(
    *, strategy_name: str, strategy_version: str, rebalance_key: str, symbol: str
) -> str:
    """A stable id, so a retried rebalance is the same order, not a second one.

    Derived only from facts that identify the intent. Nothing time-varying goes
    in, because a clock in the key would make every retry a new order and defeat
    the idempotency it exists to provide.
    """
    import hashlib

    material = f"{strategy_name}|{strategy_version}|{rebalance_key}|{symbol}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"kt-{strategy_name.lower()}-{digest}"
