"""Fractional-share execution tests.

The sizing engine is pure, so a 300-name equal-weight book is exercised here
directly rather than against a broker. That matters: the cases worth testing are
the ones where sizing goes *wrong* -- a name that cannot be traded fractionally,
a book that overruns its capital, a payload carrying both qty and notional --
and none of those should ever reach Alpaca to be discovered.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

from app.services.fractional_execution import (
    BLOCKER_INSUFFICIENT_BUYING_POWER,
    BLOCKER_INSUFFICIENT_CAPITAL,
    BLOCKER_MISSING_PRICE,
    BLOCKER_NONFRACTIONABLE,
    BLOCKER_NOT_TRADABLE,
    BLOCKER_UNKNOWN_FRACTIONABILITY,
    FRACTIONAL_POLICIES,
    SHARE_POLICY_FRACTIONAL_QTY,
    SHARE_POLICY_NOTIONAL,
    SHARE_POLICY_WHOLE,
    AssetFact,
    FractionalExecutionError,
    deterministic_client_order_id,
    plan_equal_weight_portfolio,
    preflight_assets,
    requires_fractionable_assets,
    resolve_share_policy,
    validate_order_payload,
)

CAPITAL = Decimal(100000)


def facts(symbols, *, fractionable=True, tradable=True):
    return {
        s: AssetFact(symbol=s, tradable=tradable, fractionable=fractionable)
        for s in symbols
    }


def universe(count, *, price=Decimal(100)):
    symbols = [f"SYM{i:03d}" for i in range(count)]
    return symbols, {s: price for s in symbols}


# ---------------------------------------------------------------------------
# Share policy resolution -- legacy behaviour must not move
# ---------------------------------------------------------------------------


def test_a_legacy_policy_keeps_whole_shares():
    """Every already-approved deployment carries exactly this policy shape."""
    assert resolve_share_policy({"whole_shares": True}) == SHARE_POLICY_WHOLE


def test_a_policy_with_no_share_fields_at_all_keeps_whole_shares():
    assert resolve_share_policy({}) == SHARE_POLICY_WHOLE


def test_disabling_whole_shares_without_naming_a_policy_is_refused():
    """An absent-or-false flag is not a statement about which fractional mode
    was intended, so it is not treated as one."""
    with pytest.raises(FractionalExecutionError, match="without naming a share_policy"):
        resolve_share_policy({"whole_shares": False})


def test_a_policy_asserting_both_is_refused():
    with pytest.raises(FractionalExecutionError, match="contradicts whole_shares"):
        resolve_share_policy({"whole_shares": True, "share_policy": "notional"})


def test_an_unknown_share_policy_is_refused():
    with pytest.raises(FractionalExecutionError, match="unknown share_policy"):
        resolve_share_policy({"whole_shares": False, "share_policy": "eighths"})


@pytest.mark.parametrize("policy", sorted(FRACTIONAL_POLICIES))
def test_the_fractional_policies_require_fractionable_assets(policy):
    assert requires_fractionable_assets(policy) is True
    assert requires_fractionable_assets(SHARE_POLICY_WHOLE) is False


def test_the_frozen_v1_risk_policy_is_untouched():
    """persist_policy re-hashes the dict and raises on drift, so editing v1 in
    place would break every already-approved deployment."""
    from app.services.external_execution import default_risk_policy

    policy = default_risk_policy()
    assert policy["whole_shares"] is True
    assert "share_policy" not in policy
    assert resolve_share_policy(policy) == SHARE_POLICY_WHOLE


def test_the_fractional_policy_is_a_separate_version():
    from app.services.external_execution import (
        FRACTIONAL_RISK_POLICY_VERSIONS,
        default_risk_policy,
        fractional_risk_policy,
    )

    assert set(FRACTIONAL_RISK_POLICY_VERSIONS) == {"notional", "fractional_qty"}
    for share_policy, version in FRACTIONAL_RISK_POLICY_VERSIONS.items():
        assert version != "phase10-risk-v1"
        policy = fractional_risk_policy(share_policy)
        assert policy["whole_shares"] is False
        assert policy["share_policy"] == share_policy
        # Every limit is inherited, so the two versions cannot drift apart.
        base = default_risk_policy()
        for key in base:
            if key not in {"whole_shares"}:
                assert policy[key] == base[key], key


def test_the_fractional_policy_rejects_a_whole_share_argument():
    from app.services.external_execution import fractional_risk_policy

    with pytest.raises(ValueError, match="expects one of"):
        fractional_risk_policy(SHARE_POLICY_WHOLE)


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_a_fractional_qty_payload_is_accepted():
    payload = validate_order_payload({
        "symbol": "AAPL", "qty": "3.257", "side": "buy",
        "type": "market", "time_in_force": "day",
    })
    assert payload["qty"] == "3.257"
    assert "notional" not in payload


def test_a_notional_payload_is_accepted():
    payload = validate_order_payload({
        "symbol": "AAPL", "notional": "333.33", "side": "buy",
        "type": "market", "time_in_force": "day",
    })
    assert payload["notional"] == "333.33"


def test_qty_and_notional_together_are_rejected_locally():
    """Alpaca would reject this too, but a local refusal never becomes an
    ambiguous submission we then have to reconcile."""
    with pytest.raises(FractionalExecutionError, match="mutually exclusive"):
        validate_order_payload({
            "symbol": "AAPL", "qty": "1", "notional": "100", "side": "buy",
            "type": "market", "time_in_force": "day",
        })


def test_an_order_with_neither_is_rejected():
    with pytest.raises(FractionalExecutionError, match="either qty or notional"):
        validate_order_payload({"symbol": "AAPL", "side": "buy", "type": "market"})


@pytest.mark.parametrize("field,value", [("qty", "0"), ("qty", "-1"), ("notional", "0")])
def test_a_non_positive_size_is_rejected(field, value):
    with pytest.raises(FractionalExecutionError, match="must be positive"):
        validate_order_payload({
            "symbol": "AAPL", field: value, "side": "buy",
            "type": "market", "time_in_force": "day",
        })


@pytest.mark.parametrize(
    ("order_type", "tif"), [("limit", "day"), ("market", "gtc"), ("limit", "gtc")]
)
def test_fractional_orders_are_market_day_only(order_type, tif):
    with pytest.raises(FractionalExecutionError, match="market/day only"):
        validate_order_payload({
            "symbol": "AAPL", "qty": "1.5", "side": "buy",
            "type": order_type, "time_in_force": tif,
        })


def test_a_whole_share_limit_order_is_still_allowed():
    """The new rule applies to fractional sizes only; legacy whole-share orders
    keep every order type they had."""
    payload = validate_order_payload({
        "symbol": "AAPL", "qty": "10", "side": "buy",
        "type": "limit", "time_in_force": "gtc", "limit_price": "100",
    })
    assert payload["qty"] == "10"


def test_a_fractional_bracket_is_rejected():
    with pytest.raises(FractionalExecutionError, match="order_class"):
        validate_order_payload({
            "symbol": "AAPL", "notional": "500", "side": "buy",
            "type": "market", "time_in_force": "day", "order_class": "bracket",
        })


def test_the_legacy_whole_share_bracket_still_validates():
    """The existing single-name path submits exactly this shape."""
    payload = validate_order_payload({
        "symbol": "AAPL", "qty": "7", "side": "buy", "type": "market",
        "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": "110"}, "stop_loss": {"stop_price": "95"},
    })
    assert payload["order_class"] == "bracket"


# ---------------------------------------------------------------------------
# Asset preflight
# ---------------------------------------------------------------------------


def test_a_nonfractionable_selected_asset_blocks_and_is_named():
    symbols = ["AAPL", "MSFT", "BRK.A"]
    known = facts(symbols)
    known["BRK.A"] = AssetFact("BRK.A", tradable=True, fractionable=False)
    result = preflight_assets(symbols, known, share_policy=SHARE_POLICY_NOTIONAL)
    assert result["blocked"] is True
    assert BLOCKER_NONFRACTIONABLE in result["blockers"]
    assert result["nonfractionable_symbols"] == ["BRK.A"]
    assert "BRK.A" in result["blocked_symbols"]
    assert result["nonfractionable_count"] == 1


def test_unknown_fractionability_fails_closed():
    """Never having asked Alpaca is not evidence a name can be traded
    fractionally."""
    symbols = ["AAPL", "NEWCO"]
    known = facts(symbols)
    known["NEWCO"] = AssetFact("NEWCO", tradable=True, fractionable=None)
    result = preflight_assets(symbols, known, share_policy=SHARE_POLICY_NOTIONAL)
    assert BLOCKER_UNKNOWN_FRACTIONABILITY in result["blockers"]
    assert result["unknown_fractionability_symbols"] == ["NEWCO"]


def test_a_nonfractionable_asset_does_not_block_a_whole_share_book():
    symbols = ["AAPL", "BRK.A"]
    known = facts(symbols)
    known["BRK.A"] = AssetFact("BRK.A", tradable=True, fractionable=False)
    result = preflight_assets(symbols, known, share_policy=SHARE_POLICY_WHOLE)
    assert result["blocked"] is False
    assert result["blockers"] == []


def test_an_untradable_asset_blocks_under_any_policy():
    symbols = ["AAPL", "HALTED"]
    known = facts(symbols)
    known["HALTED"] = AssetFact("HALTED", tradable=False, fractionable=True)
    for policy in (SHARE_POLICY_WHOLE, SHARE_POLICY_NOTIONAL):
        result = preflight_assets(symbols, known, share_policy=policy)
        assert BLOCKER_NOT_TRADABLE in result["blockers"]
        assert "HALTED" in result["blocked_symbols"]


def test_a_symbol_absent_from_the_asset_table_blocks():
    result = preflight_assets(
        ["AAPL", "GHOST"], facts(["AAPL"]), share_policy=SHARE_POLICY_NOTIONAL
    )
    assert BLOCKER_NOT_TRADABLE in result["blockers"]
    assert result["missing_symbols"] == ["GHOST"]


# ---------------------------------------------------------------------------
# Equal-weight sizing -- the 300-name case
# ---------------------------------------------------------------------------


def test_three_hundred_names_fit_inside_the_portfolio_capital():
    symbols, prices = universe(300)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    assert plan.blocked is False
    assert plan.selected_count == 300
    assert len(plan.orders) == 300
    assert plan.target_dollars_per_name == Decimal("333.33")
    assert plan.total_requested_notional <= CAPITAL
    assert plan.estimated_residual_cash >= 0
    # 300 x 333.33 = 99,999.00, leaving a dollar of residual cash.
    assert plan.total_requested_notional == Decimal("99999.00")
    assert plan.estimated_residual_cash == Decimal("1.00")


def test_notional_sizing_gives_exact_equal_weights():
    """Every name gets the same dollars, so the weight error is zero."""
    symbols, prices = universe(300)
    prices = {s: Decimal(str(20 + i)) for i, s in enumerate(symbols)}
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    assert Decimal(plan.diagnostics["max_absolute_weight_error"]) == 0
    assert Decimal(plan.diagnostics["max_relative_weight_error"]) == 0


def test_fractional_qty_weights_are_close_but_not_exact():
    """Rounding qty to nine decimals leaves a residue that scales with price."""
    symbols, prices = universe(300)
    prices = {s: Decimal(str(20 + i)) for i, s in enumerate(symbols)}
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_FRACTIONAL_QTY, asset_facts=facts(symbols),
    )
    assert plan.blocked is False
    # Materially tighter than a basis point of the target weight.
    assert Decimal(plan.diagnostics["max_relative_weight_error"]) < Decimal("0.0001")
    assert plan.total_requested_notional <= CAPITAL


def test_whole_share_sizing_distorts_weights_badly_at_this_size():
    """The reason this work exists: at $333/name, whole shares quantise the
    book so hard that expensive names cannot be held at all."""
    symbols, prices = universe(300)
    prices = {s: Decimal(str(20 + 2 * i)) for i, s in enumerate(symbols)}
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_WHOLE,
    )
    assert plan.diagnostics["unsized_count"] > 0  # names priced above target
    assert Decimal(plan.diagnostics["max_relative_weight_error"]) > Decimal("0.05")


def test_rounding_never_overruns_the_capital():
    """Rounding down is the whole point: to-nearest could put 300 names over
    budget by up to half a cent each."""
    for count in (7, 33, 101, 297, 300, 512):
        symbols, prices = universe(count, price=Decimal("137.77"))
        for policy in (SHARE_POLICY_NOTIONAL, SHARE_POLICY_FRACTIONAL_QTY):
            plan = plan_equal_weight_portfolio(
                symbols=symbols, reference_prices=prices,
                portfolio_capital=CAPITAL, share_policy=policy,
                asset_facts=facts(symbols),
            )
            assert plan.total_requested_notional <= CAPITAL, (count, policy)
            assert BLOCKER_INSUFFICIENT_CAPITAL not in plan.blockers


def test_target_dollars_per_name_is_capital_over_n():
    symbols, prices = universe(300)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    assert plan.target_dollars_per_name == (CAPITAL / 300).quantize(Decimal("0.01"))


def test_a_nonfractionable_name_blocks_the_whole_rebalance():
    """It is never dropped, substituted, or absorbed by redistributing its
    capital -- each of those silently edits the selection set."""
    symbols, prices = universe(300)
    known = facts(symbols)
    known["SYM042"] = AssetFact("SYM042", tradable=True, fractionable=False)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=known,
    )
    assert plan.blocked is True
    assert BLOCKER_NONFRACTIONABLE in plan.blockers
    assert "SYM042" in plan.blocked_symbols
    # The selection set is intact, and the offending name kept its full target:
    # nothing was redistributed.
    assert plan.selected_count == 300
    assert len(plan.orders) == 300
    assert all(o.target_dollars == plan.target_dollars_per_name for o in plan.orders)


def test_insufficient_buying_power_blocks():
    symbols, prices = universe(300)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
        buying_power=Decimal(50000),
    )
    assert plan.blocked is True
    assert BLOCKER_INSUFFICIENT_BUYING_POWER in plan.blockers


def test_sufficient_buying_power_does_not_block():
    symbols, prices = universe(300)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
        buying_power=Decimal(100000),
    )
    assert plan.blocked is False


def test_a_missing_reference_price_blocks_and_is_named():
    symbols, prices = universe(10)
    del prices["SYM003"]
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    assert BLOCKER_MISSING_PRICE in plan.blockers
    assert "SYM003" in plan.blocked_symbols


def test_duplicate_symbols_are_refused():
    with pytest.raises(FractionalExecutionError, match="duplicate symbols"):
        plan_equal_weight_portfolio(
            symbols=["AAPL", "AAPL"], reference_prices={"AAPL": Decimal(100)},
            portfolio_capital=CAPITAL, share_policy=SHARE_POLICY_NOTIONAL,
        )


def test_an_empty_selection_is_refused():
    with pytest.raises(FractionalExecutionError, match="at least one name"):
        plan_equal_weight_portfolio(
            symbols=[], reference_prices={}, portfolio_capital=CAPITAL,
            share_policy=SHARE_POLICY_NOTIONAL,
        )


def test_non_positive_capital_is_refused():
    with pytest.raises(FractionalExecutionError, match="must be positive"):
        plan_equal_weight_portfolio(
            symbols=["AAPL"], reference_prices={"AAPL": Decimal(100)},
            portfolio_capital=Decimal(0), share_policy=SHARE_POLICY_NOTIONAL,
        )


def test_a_weight_error_tolerance_can_block():
    symbols, prices = universe(300)
    prices = {s: Decimal(str(20 + 2 * i)) for i, s in enumerate(symbols)}
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_WHOLE,
        max_absolute_weight_error=Decimal("0.0001"),
    )
    assert plan.blocked is True


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_every_required_diagnostic_is_reported():
    symbols, prices = universe(300)
    known = facts(symbols)
    known["SYM007"] = AssetFact("SYM007", tradable=True, fractionable=False)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=known,
    )
    diagnostics = plan.diagnostics
    for key in (
        "requested_portfolio_capital",
        "selected_count",
        "target_dollars_per_name",
        "total_requested_notional",
        "estimated_residual_cash",
        "fractionable_count",
        "nonfractionable_count",
        "max_absolute_weight_error",
        "max_relative_weight_error",
    ):
        assert key in diagnostics, key
        assert diagnostics[key] is not None, key
    assert diagnostics["selected_count"] == 300
    assert diagnostics["nonfractionable_count"] == 1
    assert diagnostics["fractionable_count"] == 299


def test_the_plan_serialises_to_json_safe_values():
    import json

    symbols, prices = universe(25)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    json.dumps(plan.as_dict())  # must not raise on Decimal


# ---------------------------------------------------------------------------
# Payloads and idempotency
# ---------------------------------------------------------------------------


def test_every_generated_payload_carries_exactly_one_size_field():
    symbols, prices = universe(300)
    for policy in (SHARE_POLICY_NOTIONAL, SHARE_POLICY_FRACTIONAL_QTY):
        plan = plan_equal_weight_portfolio(
            symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
            share_policy=policy, asset_facts=facts(symbols),
        )
        for order in plan.orders:
            payload = order.payload(client_order_id="kt-test-1")
            assert ("qty" in payload) != ("notional" in payload)
            assert payload["side"] == "buy"
            assert payload["type"] == "market"
            assert payload["time_in_force"] == "day"


def test_client_order_ids_are_deterministic_and_unique_per_symbol():
    first = deterministic_client_order_id(
        strategy_name="MOM_12_1", strategy_version="1.0",
        rebalance_key="2026-09-01", symbol="AAPL",
    )
    again = deterministic_client_order_id(
        strategy_name="MOM_12_1", strategy_version="1.0",
        rebalance_key="2026-09-01", symbol="AAPL",
    )
    other = deterministic_client_order_id(
        strategy_name="MOM_12_1", strategy_version="1.0",
        rebalance_key="2026-09-01", symbol="MSFT",
    )
    assert first == again  # a retried rebalance is the same order
    assert first != other


def test_a_later_rebalance_gets_a_different_id():
    september = deterministic_client_order_id(
        strategy_name="MOM_12_1", strategy_version="1.0",
        rebalance_key="2026-09-01", symbol="AAPL",
    )
    october = deterministic_client_order_id(
        strategy_name="MOM_12_1", strategy_version="1.0",
        rebalance_key="2026-10-01", symbol="AAPL",
    )
    assert september != october


def test_client_order_ids_carry_no_clock():
    """A timestamp in the key would make every retry a new order and defeat the
    idempotency it exists to provide."""
    source = inspect.getsource(deterministic_client_order_id)
    tree = ast.parse(inspect.cleandoc(source))
    called = {
        getattr(n.func, "attr", None) or getattr(n.func, "id", None)
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for banned in ("now", "utcnow", "time", "uuid4", "random"):
        assert banned not in called


def test_a_full_three_hundred_name_order_set_is_idempotent():
    symbols, prices = universe(300)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    ids = [
        deterministic_client_order_id(
            strategy_name="MOM_12_1", strategy_version="1.0",
            rebalance_key="2026-09-01", symbol=o.symbol,
        )
        for o in plan.orders
    ]
    assert len(set(ids)) == 300  # no collisions across the book
    again = [
        deterministic_client_order_id(
            strategy_name="MOM_12_1", strategy_version="1.0",
            rebalance_key="2026-09-01", symbol=o.symbol,
        )
        for o in plan.orders
    ]
    assert ids == again


# ---------------------------------------------------------------------------
# Safety regression
# ---------------------------------------------------------------------------


def test_the_sizing_engine_only_ever_buys():
    symbols, prices = universe(50)
    plan = plan_equal_weight_portfolio(
        symbols=symbols, reference_prices=prices, portfolio_capital=CAPITAL,
        share_policy=SHARE_POLICY_NOTIONAL, asset_facts=facts(symbols),
    )
    for order in plan.orders:
        assert order.payload(client_order_id="x")["side"] == "buy"


def test_no_module_here_can_submit_or_enable_anything():
    """Structural: this module plans orders, it does not send them."""
    from app.services import fractional_execution as module

    tree = ast.parse(inspect.getsource(module))
    called = {
        getattr(n.func, "attr", None) or getattr(n.func, "id", None)
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for banned in ("submit_order", "post", "request", "_mutate", "cancel_order"):
        assert banned not in called
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("httpx", "app.brokers.alpaca_paper", "app.settings"):
        assert banned not in imported


def test_the_adapter_still_refuses_sells_and_still_needs_both_flags():
    """The new payload guard must not have displaced either existing check."""
    import app.brokers.alpaca_paper as adapter_module

    source = inspect.getsource(adapter_module.AlpacaPaperBrokerAdapter.submit_order)
    assert "position-reducing sells only" in source
    assert "both broker execution flags" in source
    assert "validate_order_payload" in source
    # The flag check must still precede the network call.
    assert source.index("both broker execution flags") < source.index("_mutate")


def test_execution_flags_remain_disabled_by_default():
    """This change adds a capability; it does not turn anything on."""
    from app.settings import Settings

    fresh = Settings(_env_file=None)
    assert fresh.broker_order_submission_enabled is False
    assert fresh.external_paper_execution_enabled is False
