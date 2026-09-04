"""Portfolio execution bridge and position-reducing sell tests.

The sell tests carry the weight here. "Allow sells" and "allow shorts" are one
keystroke apart, so every way a reduction could cross zero is exercised
explicitly -- oversized, exactly-at-zero, negative target, absent position,
stale snapshot, dirty reconciliation, and a payload that reaches the adapter
having bypassed the planner entirely.
"""

from __future__ import annotations

import ast
import csv
import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.fractional_execution import AssetFact, FractionalExecutionError
from app.services.portfolio_execution_bridge import (
    BLOCKER_NONFRACTIONABLE,
    BLOCKER_OWNERSHIP,
    BLOCKER_RECONCILIATION,
    BLOCKER_SELL_UNSAFE,
    BLOCKER_UNKNOWN_FRACTIONABILITY,
    MOM_12_1_SHARE_POLICY,
    MOM_12_1_UNIVERSE_HASH,
    PROVENANCE_FORWARD,
    PROVENANCE_TEST_REPLAY,
    STRATEGY_MOM_12_1,
    build_rebalance_plan,
    load_portfolio_signal,
    verify_plan_against_signal,
)
from app.services.position_reducing_sell import (
    BLOCKER_EXCEEDS_POSITION,
    ConfirmedPosition,
    ShortSellProhibited,
    assert_sell_is_position_reducing,
    max_position_staleness,
    plan_full_exit,
    plan_position_reduction,
)

CAPITAL = Decimal(100000)
# Freshness gates compare against wall-clock time, so the fixture must remain
# fresh regardless of when the suite is run. Signal dates stay frozen below.
NOW = datetime.now(UTC)


def evidence(status="clean", *, age=timedelta(minutes=1), run_id=4242):
    """Reconciliation evidence: a real run, its verdict, and when it ran."""
    from app.services.strategy_ownership import ReconciliationEvidence

    return ReconciliationEvidence(
        run_id=run_id, status=status, completed_at=NOW - age, broker_account_id=1
    )


def ledger(owned, *, strategy=STRATEGY_MOM_12_1, available=True):
    """A strategy ownership ledger from {symbol: quantity}."""
    from app.services.strategy_ownership import (
        StrategyOwnedPosition,
        StrategyOwnershipLedger,
    )

    if not available:
        return StrategyOwnershipLedger.unavailable(strategy, source="test")
    return StrategyOwnershipLedger(
        strategy=strategy,
        positions={
            symbol: StrategyOwnedPosition(
                strategy=strategy, symbol=symbol,
                quantity=Decimal(str(qty)), as_of=NOW - timedelta(minutes=1),
            )
            for symbol, qty in owned.items()
        },
        available=True,
        source="test",
    )


def position(symbol, qty, price=Decimal(100), *, age=timedelta(minutes=1),
             status="clean"):
    return ConfirmedPosition(
        symbol=symbol,
        quantity=Decimal(str(qty)),
        market_value=Decimal(str(qty)) * price,
        observed_at=NOW - age,
        reconciliation_status=status,
    )


def write_signal(tmp_path, symbols, *, universe_hash=MOM_12_1_UNIVERSE_HASH,
                 signal_date="2026-08-31", execution_date="2026-09-01",
                 version="1.0", name="signal.csv"):
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["symbol", "signal_date", "intended_execution_date",
             "universe_hash", "strategy_version"]
        )
        for symbol in symbols:
            writer.writerow([symbol, signal_date, execution_date, universe_hash, version])
    return path


def universe(count):
    """A synthetic selection set of ``count`` names.

    Sizes here are *sizing stress*, not claims about MOM_12_1's portfolio. The
    strategy selects the top 10% of names eligible at that month-end; a
    survivorship-aware historical test averaged about 307 selected names,
    because roughly 3,069 of the frozen 6,082 were eligible on an average
    formation date. Larger counts are exercised to stress rounding and weight
    error, not because the book is expected to be that size.
    """
    return [f"SYM{i:04d}" for i in range(count)]


# Representative of the historical average selection: ~10% of ~3,069 eligible.
TYPICAL_SELECTED_COUNT = 307


def facts(symbols, *, fractionable=True, tradable=True):
    return {
        s: AssetFact(symbol=s, tradable=tradable, fractionable=fractionable)
        for s in symbols
    }


def prices(symbols, price=Decimal(100)):
    return {s: price for s in symbols}


# ---------------------------------------------------------------------------
# Signal loading -- read, never recompute
# ---------------------------------------------------------------------------


def test_a_signal_loads_with_its_frozen_identity(tmp_path):
    path = write_signal(tmp_path, universe(608))
    signal = load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)
    assert signal.strategy == STRATEGY_MOM_12_1
    assert signal.universe_hash == MOM_12_1_UNIVERSE_HASH
    assert signal.selected_count == 608
    assert signal.target_weight == Decimal(1) / Decimal(608)
    assert signal.signal_date.isoformat() == "2026-08-31"
    assert signal.intended_execution_date.isoformat() == "2026-09-01"
    assert len(signal.source_sha256) == 64


def test_provenance_must_be_stated_and_a_replay_is_not_forward_evidence(tmp_path):
    """A historical CSV replayed for plumbing is indistinguishable on disk from
    a genuine forward signal, so the caller asserts which it is."""
    path = write_signal(tmp_path, universe(10))
    replay = load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)
    assert replay.is_forward_evidence is False
    forward = load_portfolio_signal(path, provenance=PROVENANCE_FORWARD)
    assert forward.is_forward_evidence is True
    with pytest.raises(FractionalExecutionError, match="provenance must be"):
        load_portfolio_signal(path, provenance="probably_fine")


def test_a_wrong_universe_hash_is_refused(tmp_path):
    path = write_signal(tmp_path, universe(10), universe_hash="deadbeefdeadbeef")
    with pytest.raises(FractionalExecutionError, match="does not match the frozen"):
        load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)


def test_a_duplicate_symbol_is_refused(tmp_path):
    path = write_signal(tmp_path, ["AAPL", "MSFT", "AAPL"])
    with pytest.raises(FractionalExecutionError, match="more than once"):
        load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)


def test_two_signal_dates_in_one_file_are_refused(tmp_path):
    path = tmp_path / "mixed.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "signal_date", "intended_execution_date",
                         "universe_hash", "strategy_version"])
        writer.writerow(["AAPL", "2026-08-31", "2026-09-01", MOM_12_1_UNIVERSE_HASH, "1.0"])
        writer.writerow(["MSFT", "2026-07-31", "2026-08-01", MOM_12_1_UNIVERSE_HASH, "1.0"])
    with pytest.raises(FractionalExecutionError, match="one signal is one rebalance"):
        load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)


def test_a_missing_column_is_refused(tmp_path):
    path = tmp_path / "thin.csv"
    path.write_text("symbol\nAAPL\n", encoding="utf-8")
    with pytest.raises(FractionalExecutionError, match="missing columns"):
        load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)


# ---------------------------------------------------------------------------
# The rebalance plan reproduces the signal exactly
# ---------------------------------------------------------------------------


def test_a_full_rebalance_reproduces_the_signal_exactly(tmp_path):
    symbols = universe(608)
    path = write_signal(tmp_path, symbols)
    signal = load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence(),
    )
    proof = verify_plan_against_signal(plan)
    assert proof["selection_matches_signal"] is True
    assert proof["selected_count_matches"] is True
    assert proof["target_weight_is_one_over_n"] is True
    assert proof["target_dollars_are_equal"] is True
    assert proof["total_within_allocated_capital"] is True
    assert proof["residual_within_rounding_bound"] is True
    assert proof["symbols_omitted"] == []
    assert proof["symbols_added"] == []
    assert proof["orders_submitted"] is False
    assert plan.blocked is False


def test_target_weight_is_exactly_one_over_n(tmp_path):
    for count in (10, 101, 300, 608):
        symbols = universe(count)
        signal = load_portfolio_signal(
            write_signal(tmp_path, symbols, name=f"s{count}.csv"),
            provenance=PROVENANCE_TEST_REPLAY,
        )
        plan = build_rebalance_plan(
            signal=signal, allocated_capital=CAPITAL,
            reference_prices=prices(symbols), asset_facts=facts(symbols),
            ownership=ledger({}), reconciliation=evidence(),
        )
        assert all(
            p.target_weight == Decimal(1) / Decimal(count) for p in plan.symbol_plans
        )


def test_residual_cash_is_explained_entirely_by_rounding(tmp_path):
    for count in (7, 101, 300, 608, 997):
        symbols = universe(count)
        signal = load_portfolio_signal(
            write_signal(tmp_path, symbols, name=f"r{count}.csv"),
            provenance=PROVENANCE_TEST_REPLAY,
        )
        plan = build_rebalance_plan(
            signal=signal, allocated_capital=CAPITAL,
            reference_prices=prices(symbols), asset_facts=facts(symbols),
            ownership=ledger({}), reconciliation=evidence(),
        )
        proof = verify_plan_against_signal(plan)
        assert proof["residual_within_rounding_bound"] is True, count
        assert plan.residual_cash >= 0
        # Under a cent per name, by construction of ROUND_DOWN.
        assert plan.residual_cash < Decimal("0.01") * count


def test_no_symbol_is_silently_omitted(tmp_path):
    symbols = universe(608)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence(),
    )
    assert [p.symbol for p in plan.symbol_plans] == symbols
    assert len(plan.symbol_plans) == 608


def test_every_buy_is_a_notional_market_day_order(tmp_path):
    symbols = universe(300)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence(),
    )
    for symbol_plan in plan.symbol_plans:
        payload = symbol_plan.order_payload
        assert payload is not None
        assert payload["side"] == "buy"
        assert payload["type"] == "market"
        assert payload["time_in_force"] == "day"
        assert "notional" in payload
        assert "qty" not in payload
    assert plan.share_policy == MOM_12_1_SHARE_POLICY == "notional"


def test_client_order_ids_are_deterministic_and_unique(tmp_path):
    symbols = universe(608)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    kwargs = {
        "allocated_capital": CAPITAL,
        "reference_prices": prices(symbols),
        "asset_facts": facts(symbols),
        "ownership": ledger({}),
        "reconciliation": evidence(),
    }
    first = build_rebalance_plan(signal=signal, **kwargs)
    again = build_rebalance_plan(signal=signal, **kwargs)
    ids = [p.client_order_id for p in first.symbol_plans]
    assert len(set(ids)) == 608
    assert ids == [p.client_order_id for p in again.symbol_plans]


def test_a_held_position_reduces_the_required_buy(tmp_path):
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    held = {"SYM0000": position("SYM0000", 40)}  # $4,000 of a $10,000 target
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=held,
        ownership=ledger({"SYM0000": 40}), reconciliation=evidence(),
    )
    first = plan.symbol_plans[0]
    assert first.current_dollars == Decimal(4000)
    assert first.required_dollar_delta == Decimal(6000)
    assert first.order_payload["notional"] == "6000.00"


# ---------------------------------------------------------------------------
# Fail-closed blocking
# ---------------------------------------------------------------------------


def test_one_nonfractionable_name_blocks_the_entire_rebalance(tmp_path):
    symbols = universe(608)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    known = facts(symbols)
    known["SYM0042"] = AssetFact("SYM0042", tradable=True, fractionable=False)
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=known,
        ownership=ledger({}), reconciliation=evidence(),
    )
    assert plan.blocked is True
    assert BLOCKER_NONFRACTIONABLE in plan.blockers
    assert "SYM0042" in plan.blocked_symbols
    # Nothing was dropped, substituted, or redistributed.
    assert len(plan.symbol_plans) == 608
    assert all(p.target_dollars == plan.target_dollars_per_name for p in plan.symbol_plans)
    blocked = next(p for p in plan.symbol_plans if p.symbol == "SYM0042")
    assert blocked.order_payload is None
    assert blocked.target_weight == signal.target_weight


def test_unknown_fractionability_blocks_the_rebalance(tmp_path):
    symbols = universe(50)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    known = facts(symbols)
    known["SYM0003"] = AssetFact("SYM0003", tradable=True, fractionable=None)
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=known,
        ownership=ledger({}), reconciliation=evidence(),
    )
    assert plan.blocked is True
    assert BLOCKER_UNKNOWN_FRACTIONABILITY in plan.blockers


def test_a_dirty_reconciliation_blocks_the_rebalance(tmp_path):
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence("drifted"),
    )
    assert plan.blocked is True
    assert BLOCKER_RECONCILIATION in plan.blockers


def test_insufficient_buying_power_blocks(tmp_path):
    symbols = universe(300)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        buying_power=Decimal(1000),
        ownership=ledger({}), reconciliation=evidence(),
    )
    assert plan.blocked is True


# ---------------------------------------------------------------------------
# Position-reducing sells -- crossing zero must be impossible
# ---------------------------------------------------------------------------


def test_a_reduction_leaves_a_non_negative_position():
    order = plan_position_reduction(
        position=position("AAPL", 100), target_dollars=Decimal(3000),
        reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
    )
    assert order.sell_qty == Decimal(70)
    assert order.resulting_quantity == Decimal(30)
    assert order.closes_position is False


def test_a_full_exit_sells_exactly_the_confirmed_quantity():
    order = plan_full_exit(position=position("AAPL", 37.5), reconciliation=evidence(), now=NOW)
    assert order.sell_qty == Decimal("37.5")
    assert order.resulting_quantity == 0
    assert order.closes_position is True


def test_selling_more_than_held_is_refused_at_the_construction_gate():
    """Every path into a sell funnels through _build_reduction, so the
    cannot-cross-zero invariant is enforced once rather than restated at each
    caller. This calls it directly with an oversized quantity."""
    from app.services.position_reducing_sell import _build_reduction

    with pytest.raises(ShortSellProhibited, match="would open a short"):
        _build_reduction(position("AAPL", 10), Decimal("10.000000001"))
    # Exactly the held quantity is a full exit, not a short.
    assert _build_reduction(position("AAPL", 10), Decimal(10)).resulting_quantity == 0


def test_the_error_names_the_short_that_would_have_been_opened():
    from app.services.position_reducing_sell import _build_reduction

    with pytest.raises(ShortSellProhibited, match=BLOCKER_EXCEEDS_POSITION):
        _build_reduction(position("AAPL", 10), Decimal(25))


def test_a_negative_target_is_refused_as_a_short():
    with pytest.raises(ShortSellProhibited, match="would require a short"):
        plan_position_reduction(
            position=position("AAPL", 10), target_dollars=Decimal(-500),
            reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
        )


def test_there_is_no_position_to_reduce():
    with pytest.raises(ShortSellProhibited, match="no confirmed long position"):
        plan_position_reduction(
            position=None, target_dollars=Decimal(0),
            reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
        )
    with pytest.raises(ShortSellProhibited, match="no confirmed long position"):
        plan_full_exit(position=None, reconciliation=evidence(), now=NOW)


def test_a_non_long_position_cannot_be_reduced():
    for quantity in (0, -5):
        with pytest.raises(ShortSellProhibited, match="not a long position|holds"):
            plan_position_reduction(
                position=position("AAPL", quantity), target_dollars=Decimal(0),
                reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
            )


def test_a_stale_position_snapshot_is_refused():
    """Selling against a remembered position is how an account ends up short."""
    stale = position("AAPL", 100, age=max_position_staleness() + timedelta(seconds=1))
    with pytest.raises(ShortSellProhibited, match="stale|older than"):
        plan_position_reduction(
            position=stale, target_dollars=Decimal(3000),
            reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
        )
    with pytest.raises(ShortSellProhibited, match="stale"):
        plan_full_exit(position=stale, reconciliation=evidence(), now=NOW)


def test_a_fresh_snapshot_at_the_boundary_is_accepted():
    boundary = position("AAPL", 100, age=max_position_staleness())
    order = plan_position_reduction(
        position=boundary, target_dollars=Decimal(3000),
        reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
    )
    assert order.sell_qty == Decimal(70)


def test_a_dirty_reconciliation_blocks_a_sell():
    from app.services.strategy_ownership import ReconciliationEvidenceMissing

    with pytest.raises(ReconciliationEvidenceMissing, match="not\\s+clean"):
        plan_position_reduction(
            position=position("AAPL", 100), target_dollars=Decimal(3000),
            reference_price=Decimal(100), reconciliation=evidence("drifted"),
            now=NOW,
        )


def test_a_target_at_or_above_the_holding_is_not_a_reduction():
    with pytest.raises(FractionalExecutionError, match="not a reduction"):
        plan_position_reduction(
            position=position("AAPL", 10), target_dollars=Decimal(1000),
            reference_price=Decimal(100), reconciliation=evidence(), now=NOW,
        )


def test_a_naive_timestamp_is_refused():
    naive = ConfirmedPosition(
        symbol="AAPL", quantity=Decimal(10), market_value=Decimal(1000),
        observed_at=datetime(2026, 9, 1, 14, 30),  # noqa: DTZ001 -- the point
            )
    with pytest.raises(FractionalExecutionError, match="carries no timezone"):
        plan_full_exit(position=naive, reconciliation=evidence(), now=NOW)


# ---------------------------------------------------------------------------
# The adapter's last-mile sell guard
# ---------------------------------------------------------------------------


def test_a_sell_payload_bypassing_the_planner_is_still_checked():
    """The adapter cannot see the plan that produced an order, so the invariant
    is re-derived from the payload and the confirmed book."""
    book = {"AAPL": position("AAPL", 10)}
    assert_sell_is_position_reducing(
        {"symbol": "AAPL", "side": "sell", "qty": "10"}, book
    )
    with pytest.raises(ShortSellProhibited, match="would open a short"):
        assert_sell_is_position_reducing(
            {"symbol": "AAPL", "side": "sell", "qty": "10.000000001"}, book
        )


def test_a_sell_for_an_unheld_symbol_is_refused():
    with pytest.raises(ShortSellProhibited, match="no confirmed long position"):
        assert_sell_is_position_reducing(
            {"symbol": "TSLA", "side": "sell", "qty": "1"}, {"AAPL": position("AAPL", 10)}
        )


def test_a_notional_sell_is_refused():
    """A dollar amount cannot be bounded by a share count without guessing the
    fill price."""
    with pytest.raises(ShortSellProhibited, match="expressed in shares"):
        assert_sell_is_position_reducing(
            {"symbol": "AAPL", "side": "sell", "notional": "500"},
            {"AAPL": position("AAPL", 10)},
        )


def test_the_guard_ignores_buy_payloads():
    payload = {"symbol": "AAPL", "side": "buy", "notional": "500"}
    assert assert_sell_is_position_reducing(payload, {}) is payload


def test_the_adapter_refuses_a_sell_without_the_confirmed_book(monkeypatch):
    """Even with both flags on and a live client, a sell missing any of its
    three evidences -- confirmed book, ownership ledger, reconciliation -- cannot
    be shown safe, so it is refused before the network."""
    import asyncio

    import httpx

    from app.brokers.base import BrokerMutationDisabled
    from app.services.strategy_ownership import (
        OwnershipUnavailable,
        ReconciliationEvidenceMissing,
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "should-never-happen"})

    # Through the governed submitter: attribution succeeds, and the adapter's
    # own evidence checks still refuse. Being attributable is not being safe.
    adapter, client = _paper_adapter(monkeypatch, handler)
    sell = _sell("AAPL", 1)
    book = {"AAPL": position("AAPL", 10)}
    owned = ledger({"AAPL": 10})

    async def run() -> None:
        with pytest.raises(BrokerMutationDisabled, match="confirmed long"):
            await adapter.submit_order(sell)
        with pytest.raises(ReconciliationEvidenceMissing):
            await adapter.submit_order(
                sell, confirmed_positions=book, ownership_ledger=owned
            )
        with pytest.raises(OwnershipUnavailable):
            await adapter.submit_order(
                sell, confirmed_positions=book, reconciliation=evidence()
            )
        await client.aclose()

    asyncio.run(run())
    assert requests == []  # nothing reached the network


def test_the_adapter_still_names_buy_and_reducing_sells_only():
    import app.brokers.alpaca_paper as module

    frozen = inspect.getsource(module.AlpacaPaperBrokerAdapter.submit_order)
    assert "buy orders only" in frozen  # the frozen release did not gain sells
    source = inspect.getsource(
        module.AlpacaPaperPortfolioAdapter._submit_governed_order
    )
    assert "position-reducing sells only" in source
    assert "both broker execution flags" in source
    assert "assert_sell_is_position_reducing" in source
    # The flag gate still precedes the network call.
    assert source.index("both broker execution flags") < source.index("_mutate")


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


def test_a_dropped_name_becomes_an_exit(tmp_path):
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    held = {"OLDCO": position("OLDCO", 25)}
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=held,
        ownership=ledger({"OLDCO": 25}), reconciliation=evidence(),
    )
    assert len(plan.exits) == 1
    exit_plan = plan.exits[0]
    assert exit_plan.symbol == "OLDCO"
    assert exit_plan.action == "exit"
    assert exit_plan.target_weight == 0
    assert exit_plan.order_payload["side"] == "sell"
    assert exit_plan.order_payload["qty"] == "25"
    assert "notional" not in exit_plan.order_payload


def test_an_unsafe_exit_blocks_rather_than_selling(tmp_path):
    """The strategy owns the dropped name, but reconciliation is dirty, so no
    sell may be constructed for it."""
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    held = {"OLDCO": position("OLDCO", 25)}
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=held,
        ownership=ledger({"OLDCO": 25}), reconciliation=evidence("drifted"),
    )
    assert BLOCKER_SELL_UNSAFE in plan.blockers
    assert plan.exits[0].order_payload is None


def test_a_still_selected_name_is_never_exited(tmp_path):
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    held = {s: position(s, 5) for s in symbols}
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=held,
        ownership=ledger(dict.fromkeys(symbols, 5)), reconciliation=evidence(),
    )
    assert plan.exits == ()


# ---------------------------------------------------------------------------
# Nothing here submits
# ---------------------------------------------------------------------------


def test_the_bridge_cannot_submit_an_order():
    from app.services import portfolio_execution_bridge as module

    tree = ast.parse(inspect.getsource(module))
    called = {
        getattr(n.func, "attr", None) or getattr(n.func, "id", None)
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for banned in ("submit_order", "_mutate", "post", "request", "cancel_order"):
        assert banned not in called
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("httpx", "app.brokers.alpaca_paper"):
        assert banned not in imported


def test_the_sell_module_cannot_submit_an_order():
    """It reads settings for the configurable staleness bound, which is not a
    capability -- but it must reach no broker and no HTTP client, so it can
    never be the thing that sends an order."""
    from app.services import position_reducing_sell as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("httpx", "app.brokers.alpaca_paper", "app.brokers.base"):
        assert banned not in imported
    called = {
        getattr(n.func, "attr", None) or getattr(n.func, "id", None)
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for banned in ("submit_order", "_mutate", "post", "request"):
        assert banned not in called


def test_every_plan_declares_itself_observe_only(tmp_path):
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence(),
    )
    payload = plan.as_dict()
    assert payload["observe_only"] is True
    assert payload["orders_submitted"] is False

    json.dumps(payload)  # must not raise on Decimal or date


def test_execution_flags_remain_off():
    from app.settings import Settings

    fresh = Settings(_env_file=None)
    assert fresh.broker_order_submission_enabled is False
    assert fresh.external_paper_execution_enabled is False


def test_a_typical_sized_selection_sizes_cleanly(tmp_path):
    """The size the strategy actually tends to produce: ~307 names, being 10%
    of the ~3,069 eligible on an average formation date."""
    symbols = universe(TYPICAL_SELECTED_COUNT)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols, name="typical.csv"),
        provenance=PROVENANCE_TEST_REPLAY,
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence(),
    )
    proof = verify_plan_against_signal(plan)
    assert plan.signal.selected_count == 307
    assert len(plan.symbol_plans) == 307
    assert plan.target_dollars_per_name == Decimal("325.73")  # 100000/307 down
    assert proof["target_weight_is_one_over_n"] is True
    assert proof["residual_within_rounding_bound"] is True
    assert proof["total_within_allocated_capital"] is True
    assert plan.blocked is False


def test_selection_count_is_never_assumed_by_the_bridge():
    """The bridge takes N from the signal. Nothing in it encodes a portfolio
    size, so a month selecting 280 or 340 names is handled identically."""
    from app.services import portfolio_execution_bridge as module

    source = inspect.getsource(module)
    for hardcoded in ("307", "608", "top 10", "0.10"):
        assert hardcoded not in source, hardcoded


# ---------------------------------------------------------------------------
# Strategy ownership -- the account book is not MOM's holdings
# ---------------------------------------------------------------------------


def test_an_unrelated_account_position_is_never_exited(tmp_path):
    """The account holds LEGACY. MOM_12_1 does not own it and did not select
    it. MOM must generate no exit for it -- the position belongs to whoever put
    it there, and MOM ceasing to select a symbol is not a claim on it."""
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    account_book = {
        "LEGACY": position("LEGACY", 500),        # someone else's holding
        "OTHERSTRAT": position("OTHERSTRAT", 80),
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=account_book,
        ownership=ledger({}),  # MOM owns nothing yet
        reconciliation=evidence(),
    )
    assert plan.exits == ()
    exited = {p.symbol for p in plan.exits}
    assert "LEGACY" not in exited
    assert "OTHERSTRAT" not in exited
    # And the plan says plainly that it saw those positions without claiming them.
    assert plan.diagnostics["account_positions_seen"] == ["LEGACY", "OTHERSTRAT"]
    assert plan.diagnostics["strategy_owned_symbols"] == []
    assert plan.diagnostics["account_positions_are_not_ownership"] is True


def test_only_owned_names_are_exited_when_the_account_holds_more(tmp_path):
    """MOM owns OLDMOM; the account also holds LEGACY. Exactly one exit."""
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    account_book = {
        "OLDMOM": position("OLDMOM", 30),
        "LEGACY": position("LEGACY", 500),
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=account_book,
        ownership=ledger({"OLDMOM": 30}),
        reconciliation=evidence(),
    )
    assert [p.symbol for p in plan.exits] == ["OLDMOM"]
    assert plan.exits[0].order_payload["qty"] == "30"


def test_a_shared_symbol_reduces_only_the_attributed_quantity(tmp_path):
    """The harder case: MOM and another strategy both hold AAPL.

    The account shows 100 shares; MOM is attributed 40. MOM dropping AAPL may
    sell 40 and no more -- the other 60 are not MOM's to liquidate.
    """
    symbols = universe(5)  # AAPL deliberately not selected this month
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    account_book = {"AAPL": position("AAPL", 100)}  # both strategies combined
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=account_book,
        ownership=ledger({"AAPL": 40}),  # MOM's attributed share
        reconciliation=evidence(),
    )
    assert len(plan.exits) == 1
    exit_plan = plan.exits[0]
    assert exit_plan.symbol == "AAPL"
    assert exit_plan.order_payload["qty"] == "40"  # not 100
    assert exit_plan.current_quantity == Decimal(40)


def test_sellable_quantity_is_the_lesser_of_owned_and_available():
    """The two bounds answer different questions; neither substitutes."""
    from app.services.strategy_ownership import sellable_quantity

    owned = ledger({"AAPL": 40})
    assert sellable_quantity(
        strategy=STRATEGY_MOM_12_1, symbol="AAPL", ledger=owned,
        broker_quantity=Decimal(100),
    ) == Decimal(40)   # ownership binds
    assert sellable_quantity(
        strategy=STRATEGY_MOM_12_1, symbol="AAPL", ledger=owned,
        broker_quantity=Decimal(25),
    ) == Decimal(25)   # availability binds


def test_an_unowned_symbol_can_never_be_sold():
    from app.services.strategy_ownership import (
        BLOCKER_NOT_OWNED,
        OwnershipUnavailable,
        sellable_quantity,
    )

    with pytest.raises(OwnershipUnavailable, match=BLOCKER_NOT_OWNED):
        sellable_quantity(
            strategy=STRATEGY_MOM_12_1, symbol="LEGACY",
            ledger=ledger({"AAPL": 10}), broker_quantity=Decimal(500),
        )


def test_selling_more_than_the_attributed_quantity_is_refused():
    from app.services.strategy_ownership import (
        BLOCKER_EXCEEDS_OWNED,
        OwnershipUnavailable,
        assert_within_strategy_ownership,
    )

    owned = ledger({"AAPL": 40})
    assert_within_strategy_ownership(
        strategy=STRATEGY_MOM_12_1, symbol="AAPL",
        requested_qty=Decimal(40), ledger=owned,
    )
    with pytest.raises(OwnershipUnavailable, match=BLOCKER_EXCEEDS_OWNED):
        assert_within_strategy_ownership(
            strategy=STRATEGY_MOM_12_1, symbol="AAPL",
            requested_qty=Decimal("40.000000001"), ledger=owned,
        )


def test_an_unavailable_ledger_blocks_the_rebalance(tmp_path):
    """Absent ownership is not 'owns nothing'. It is the absence of a claim,
    and no exit may be planned from it."""
    from app.services.portfolio_execution_bridge import BLOCKER_OWNERSHIP

    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions={"OLDCO": position("OLDCO", 25)},
        ownership=ledger({}, available=False),
        reconciliation=evidence(),
    )
    assert plan.blocked is True
    assert BLOCKER_OWNERSHIP in plan.blockers
    assert plan.exits == ()  # no exit invented from the account book


def test_the_adapter_refuses_a_sell_beyond_strategy_ownership(monkeypatch):
    """Ownership is enforced at the mutation boundary too, against the fresh
    read: the broker holding more does not widen what MOM may sell."""
    import asyncio

    import httpx

    from app.services.strategy_ownership import OwnershipUnavailable

    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=_position_body("AAPL", 100))
        posted.append(request.url.path)
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(OwnershipUnavailable, match="belongs to another strategy"):
            await adapter.submit_order(
                {"symbol": "AAPL", "side": "sell", "qty": "60",
                 "type": "market", "time_in_force": "day",
                 "strategy": STRATEGY_MOM_12_1,
                 "client_order_id": "kt-mom_12_1-sell-AAPL-60"},
                confirmed_positions={"AAPL": position("AAPL", 100)},
                ownership_ledger=ledger({"AAPL": 40}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert posted == []


# ---------------------------------------------------------------------------
# Reconciliation evidence is never a default
# ---------------------------------------------------------------------------


def test_a_sell_with_everything_but_reconciliation_is_refused(monkeypatch):
    """Confirmed position present, fresh broker position sufficient,
    reconciliation argument omitted => refusal, zero POST."""
    import asyncio

    import httpx

    from app.services.strategy_ownership import ReconciliationEvidenceMissing

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=_position_body("AAPL", 100))
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(
            ReconciliationEvidenceMissing, match="absence is not cleanliness"
        ):
            await adapter.submit_order(
                {"symbol": "AAPL", "side": "sell", "qty": "10",
                 "type": "market", "time_in_force": "day",
                 "strategy": STRATEGY_MOM_12_1,
                 "client_order_id": "kt-mom_12_1-sell-AAPL-10"},
                confirmed_positions={"AAPL": position("AAPL", 100)},
                ownership_ledger=ledger({"AAPL": 40}),
                # reconciliation deliberately omitted
            )
        await client.aclose()

    asyncio.run(run())
    assert not any("POST" in call for call in calls)


def test_no_sell_path_defaults_reconciliation_to_clean():
    """Structural: a default of "clean" would mean every caller that forgot the
    argument asserted cleanliness by omission."""
    import inspect as _inspect

    from app.services import position_reducing_sell as sell_module

    for name in ("plan_position_reduction", "plan_full_exit",
                 "revalidate_reduction_against_fresh_positions"):
        signature = _inspect.signature(getattr(sell_module, name))
        parameter = signature.parameters["reconciliation"]
        assert parameter.default is _inspect.Parameter.empty, name


def test_reconciliation_evidence_carries_a_run_id_and_timestamp():
    from app.services.strategy_ownership import require_clean_reconciliation

    good = evidence()
    assert require_clean_reconciliation(good) is good
    assert good.as_dict()["run_id"] == 4242
    assert good.as_dict()["is_clean"] is True


def test_stale_reconciliation_evidence_is_refused():
    from app.services.strategy_ownership import (
        ReconciliationEvidenceMissing,
        require_clean_reconciliation,
    )

    old = evidence(age=timedelta(hours=2))
    with pytest.raises(ReconciliationEvidenceMissing, match="beyond the"):
        require_clean_reconciliation(old, max_age=timedelta(minutes=10), now=NOW)


# ---------------------------------------------------------------------------
# Adapter release governance
# ---------------------------------------------------------------------------


def test_the_frozen_adapter_did_not_gain_sell_capability():
    """A new mutation capability must not ship under the approved identity."""
    from app.brokers.alpaca_paper import AlpacaPaperBrokerAdapter

    assert AlpacaPaperBrokerAdapter.adapter_version == "1.0.0"
    assert AlpacaPaperBrokerAdapter.behavior_version == "1"
    assert AlpacaPaperBrokerAdapter.change_class == "compatible_patch"
    source = inspect.getsource(AlpacaPaperBrokerAdapter.submit_order)
    assert "buy orders only" in source
    assert "assert_sell_is_position_reducing" not in source


def test_the_portfolio_adapter_is_a_separate_behavioral_release():
    from app.brokers.alpaca_paper import (
        AlpacaPaperBrokerAdapter,
        AlpacaPaperPortfolioAdapter,
    )

    assert AlpacaPaperPortfolioAdapter.adapter_version != (
        AlpacaPaperBrokerAdapter.adapter_version
    )
    assert AlpacaPaperPortfolioAdapter.behavior_version == "2"
    assert AlpacaPaperPortfolioAdapter.change_class == "behavioral_change"
    assert "position_reducing_sell" in AlpacaPaperPortfolioAdapter.capabilities


def test_an_existing_deployment_cannot_inherit_sell_capability():
    """enable_observe_only admits only compatible_patch, so the behavioral
    portfolio release cannot be approved through the existing path."""
    from app.brokers.alpaca_paper import AlpacaPaperPortfolioAdapter
    from app.services import external_execution

    source = inspect.getsource(external_execution.enable_observe_only)
    assert 'compatible_patch' in source
    assert AlpacaPaperPortfolioAdapter.change_class != "compatible_patch"


def test_the_portfolio_release_change_class_is_a_known_one():
    from app.brokers.alpaca_paper import AlpacaPaperPortfolioAdapter
    from app.services.broker_sync import persist_adapter_release

    known = inspect.getsource(persist_adapter_release)
    assert AlpacaPaperPortfolioAdapter.change_class in known


# ---------------------------------------------------------------------------
# Signal identity
# ---------------------------------------------------------------------------


def test_mixed_strategy_versions_are_refused(tmp_path):
    path = tmp_path / "mixed_version.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "signal_date", "intended_execution_date",
                         "universe_hash", "strategy_version"])
        writer.writerow(
            ["AAPL", "2026-08-31", "2026-09-01", MOM_12_1_UNIVERSE_HASH, "1.0"]
        )
        writer.writerow(
            ["MSFT", "2026-08-31", "2026-09-01", MOM_12_1_UNIVERSE_HASH, "1.1"]
        )
    with pytest.raises(
        FractionalExecutionError, match="one signal is one strategy version"
    ):
        load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)


def test_an_empty_strategy_version_is_refused(tmp_path):
    path = write_signal(tmp_path, universe(3), version="", name="blank_version.csv")
    with pytest.raises(FractionalExecutionError, match="empty strategy_version"):
        load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)


def test_a_forward_signal_may_not_be_version_unknown(tmp_path):
    """Forward evidence has to name the version that produced it."""
    path = write_signal(tmp_path, universe(3), version="unknown", name="unknown.csv")
    with pytest.raises(FractionalExecutionError, match="may not carry strategy_version"):
        load_portfolio_signal(path, provenance=PROVENANCE_FORWARD)
    # A replay may, because it is explicitly not forward evidence.
    replay = load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)
    assert replay.strategy_version == "unknown"
    assert replay.is_forward_evidence is False


def test_a_named_version_is_kept_verbatim(tmp_path):
    path = write_signal(tmp_path, universe(3), version="2026.08.1", name="named.csv")
    signal = load_portfolio_signal(path, provenance=PROVENANCE_FORWARD)
    assert signal.strategy_version == "2026.08.1"


# ---------------------------------------------------------------------------
# The mutation boundary -- time-of-check to time-of-use
# ---------------------------------------------------------------------------
#
# Every guard elsewhere runs against a snapshot. These run against the broker,
# at the instant of the mutation, because a snapshot read one second ago is
# already history by the time an order reaches the venue.


class _AttributionDB:
    """The smallest store the submitter needs: one attribution table.

    Real SQLite rather than a stub, so the idempotent insert and the read-back
    behave as they will in production -- these tests submit through the governed
    path, and a stub would agree with it whatever it did.
    """

    def __init__(self):
        import sqlite3

        self._db = sqlite3.connect(":memory:", isolation_level="DEFERRED")
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            "CREATE TABLE strategy_order_attributions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, broker_account_id INTEGER NOT NULL,"
            "client_order_id TEXT NOT NULL, strategy TEXT NOT NULL,"
            "strategy_version TEXT NOT NULL, symbol TEXT NOT NULL,"
            "intended_side TEXT NOT NULL, created_at TIMESTAMP NOT NULL,"
            "UNIQUE (broker_account_id, client_order_id))"
        )
        self._db.commit()

    def execute(self, query, params=()):
        from datetime import datetime as _dt
        from decimal import Decimal as _D

        adapted = tuple(str(p) if isinstance(p, (_D, _dt)) else p for p in params)
        cursor = self._db.execute(query.replace("%s", "?"), adapted)

        class _Result:
            rowcount = cursor.rowcount

            @staticmethod
            def fetchall():
                return [dict(row) for row in cursor.fetchall()]

        return _Result()

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()


class _GovernedAdapter:
    """Exposes ``submit_order`` by routing through GovernedOrderSubmitter.

    The adapter's own entry point is capability-gated now, so these tests reach
    it the way production does: attribution is persisted and verified first, and
    only then does the adapter see the order. Every check they were written to
    exercise still runs, one layer further in.
    """

    def __init__(self, adapter, strategy=None):
        from app.services.governed_order_submission import GovernedOrderSubmitter

        self.adapter = adapter
        self._strategy = strategy or STRATEGY_MOM_12_1
        self.submitter = GovernedOrderSubmitter(
            conn=_AttributionDB(), adapter=adapter, broker_account_id=1
        )

    async def submit_order(self, payload, **kwargs):
        return await self.submitter.submit(
            payload,
            strategy=str(payload.get("strategy") or self._strategy),
            strategy_version="1.0.0",
            **kwargs,
        )


def _paper_adapter(monkeypatch, handler):
    """A governed submitter over a portfolio adapter on a mock transport."""
    import httpx

    from app.brokers.alpaca_paper import AlpacaPaperPortfolioAdapter
    from app.settings import settings

    monkeypatch.setattr(settings, "broker_provider", "alpaca")
    monkeypatch.setattr(
        settings, "alpaca_paper_base_url", "https://paper-api.alpaca.markets"
    )
    monkeypatch.setattr(settings, "alpaca_paper_api_key", "paper-key")
    monkeypatch.setattr(settings, "alpaca_paper_secret_key", "paper-secret")
    monkeypatch.setattr(settings, "broker_order_submission_enabled", True)
    monkeypatch.setattr(settings, "external_paper_execution_enabled", True)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=settings.alpaca_paper_base_url,
    )
    return _GovernedAdapter(AlpacaPaperPortfolioAdapter(client=client)), client


def _position_body(symbol, qty, price=100):
    return [{"symbol": symbol, "qty": str(qty), "market_value": str(qty * price)}]


def _sell(symbol, qty):
    return {
        "symbol": symbol, "side": "sell", "qty": str(qty),
        "type": "market", "time_in_force": "day", "strategy": STRATEGY_MOM_12_1,
        # Attribution is keyed on this, so a governed order carries one.
        "client_order_id": f"kt-mom_12_1-sell-{symbol}-{qty}",
    }


def test_a_position_that_shrank_between_planning_and_submit_is_rejected(monkeypatch):
    """The case a staleness bound cannot catch.

    The stored snapshot says 10 shares, the broker now says 6, and the plan asks
    to sell 8. Rejected outright -- never clamped to 6 -- and nothing reaches
    POST /v2/orders.
    """
    import asyncio

    import httpx

    from app.services.position_reducing_sell import StalePositionAtSubmit

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=_position_body("AAPL", 6))
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(StalePositionAtSubmit, match="would open a short"):
            await adapter.submit_order(
                _sell("AAPL", 8),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert "GET /v2/positions" in seen
    assert not any("POST" in call for call in seen)  # zero mutation requests


def test_an_oversized_sell_is_never_clamped_down(monkeypatch):
    """Submitting a smaller order the strategy never asked for would silently
    change the rebalance. Reject, so it is recomputed."""
    import asyncio

    import httpx

    from app.services.position_reducing_sell import StalePositionAtSubmit

    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=_position_body("AAPL", 6))
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(StalePositionAtSubmit):
            await adapter.submit_order(
                _sell("AAPL", 8),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert posted == []  # nothing submitted, at any size


def test_a_position_that_disappeared_before_submit_is_rejected(monkeypatch):
    import asyncio

    import httpx

    from app.services.position_reducing_sell import StalePositionAtSubmit

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=[])  # the book is empty now
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(StalePositionAtSubmit, match="no longer a broker position"):
            await adapter.submit_order(
                _sell("AAPL", 5),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert not any("POST" in call for call in seen)


def test_a_shrunken_position_is_rejected_even_when_the_order_would_fit(monkeypatch):
    """10 -> 7 shares with a sell of 5: the order fits, but the plan that
    produced it rested on state that has moved, so it is recomputed."""
    import asyncio

    import httpx

    from app.services.position_reducing_sell import StalePositionAtSubmit

    adapter, client = _paper_adapter(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json=_position_body("AAPL", 7)
            if request.url.path == "/v2/positions"
            else {"id": "must-not-happen"},
        ),
    )

    async def run() -> None:
        with pytest.raises(StalePositionAtSubmit, match="shrank from"):
            await adapter.submit_order(
                _sell("AAPL", 5),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())


def test_a_failed_fresh_read_refuses_rather_than_sending_blind(monkeypatch):
    import asyncio

    import httpx

    from app.services.position_reducing_sell import StalePositionAtSubmit

    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            return httpx.Response(503)  # the read fails
        posted.append(request.url.path)
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(StalePositionAtSubmit, match="could not re-read"):
            await adapter.submit_order(
                _sell("AAPL", 5),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert posted == []


def test_a_position_that_flipped_short_is_rejected(monkeypatch):
    import asyncio

    import httpx

    from app.services.position_reducing_sell import StalePositionAtSubmit

    adapter, client = _paper_adapter(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json=_position_body("AAPL", -3)
            if request.url.path == "/v2/positions"
            else {"id": "must-not-happen"},
        ),
    )

    async def run() -> None:
        with pytest.raises(StalePositionAtSubmit, match="not a long position"):
            await adapter.submit_order(
                _sell("AAPL", 1),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())


def test_an_unchanged_position_passes_the_boundary(monkeypatch):
    """The gate must not be so strict that a correct reduction cannot proceed."""
    import asyncio

    import httpx

    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=_position_body("AAPL", 10))
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "paper-order-1"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        response = await adapter.submit_order(
            _sell("AAPL", 4),
            confirmed_positions={"AAPL": position("AAPL", 10)},
            ownership_ledger=ledger({"AAPL": 10}),
            reconciliation=evidence(),
        )
        assert response.payload["id"] == "paper-order-1"
        await client.aclose()

    asyncio.run(run())
    assert len(posted) == 1
    assert posted[0]["qty"] == "4"        # exactly what was planned, unclamped
    assert "strategy" not in posted[0]    # internal field never sent to Alpaca


def test_a_grown_position_still_passes(monkeypatch):
    """Growth cannot make a reduction cross zero, and refusing would block a
    legitimate rebalance."""
    import asyncio

    import httpx

    adapter, client = _paper_adapter(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json=_position_body("AAPL", 15)
            if request.url.path == "/v2/positions"
            else {"id": "paper-order-1"},
        ),
    )

    async def run() -> None:
        response = await adapter.submit_order(
            _sell("AAPL", 4),
            confirmed_positions={"AAPL": position("AAPL", 10)},
            ownership_ledger=ledger({"AAPL": 10}),
            reconciliation=evidence(),
        )
        assert response.payload["id"] == "paper-order-1"
        await client.aclose()

    asyncio.run(run())


def test_a_buy_does_not_trigger_a_position_read(monkeypatch):
    """The fresh read bounds a sell. A buy cannot open a short, so paying for
    the round trip would be cost without safety."""
    import asyncio

    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": "paper-order-1"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        await adapter.submit_order(
            {"symbol": "AAPL", "side": "buy", "notional": "164.47",
             "type": "market", "time_in_force": "day",
             "client_order_id": "kt-mom_12_1-buy-AAPL"}
        )
        await client.aclose()

    asyncio.run(run())
    assert seen == ["/v2/orders"]


def test_flags_turned_off_after_construction_stop_the_sell(monkeypatch):
    """Flags are re-asserted at the boundary, not trusted from construction."""
    import asyncio

    import httpx

    from app.brokers.base import BrokerMutationDisabled
    from app.settings import settings

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_position_body("AAPL", 10))

    adapter, client = _paper_adapter(monkeypatch, handler)
    monkeypatch.setattr(settings, "broker_order_submission_enabled", False)

    async def run() -> None:
        with pytest.raises(BrokerMutationDisabled, match="both broker execution flags"):
            await adapter.submit_order(
                _sell("AAPL", 4),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert "/v2/orders" not in seen


# --- staleness is configurable, and is not the authority -------------------


def test_the_default_staleness_is_three_minutes():
    """The broker worker syncs about once a minute, so 180s is a few cycles."""
    from app.services.position_reducing_sell import (
        DEFAULT_POSITION_STALENESS_SECONDS,
        max_position_staleness,
    )
    from app.settings import Settings

    assert DEFAULT_POSITION_STALENESS_SECONDS == 180
    assert Settings(_env_file=None).broker_position_snapshot_max_staleness_seconds == 180
    assert max_position_staleness() == timedelta(seconds=180)


def test_staleness_is_read_at_call_time_not_frozen_at_import(monkeypatch):
    from app.services.position_reducing_sell import max_position_staleness
    from app.settings import settings

    monkeypatch.setattr(settings, "broker_position_snapshot_max_staleness_seconds", 30)
    assert max_position_staleness() == timedelta(seconds=30)
    stale = position("AAPL", 10, age=timedelta(seconds=31))
    with pytest.raises(ShortSellProhibited, match="stale"):
        plan_full_exit(position=stale, reconciliation=evidence(), now=NOW)


# ---------------------------------------------------------------------------
# The account-aware preflight
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload


def _fake_alpaca(monkeypatch, *, account=None, positions=None, assets=None):
    """Stub the paper adapter and record every endpoint it is asked for."""
    import app.brokers.alpaca_paper as module

    calls: list[str] = []
    account = account if account is not None else {
        "id": "paper-account", "status": "ACTIVE",
        "buying_power": "100000", "cash": "100000", "trading_blocked": False,
    }

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        async def _get(self, path, label):
            calls.append(path)
            if path == "/v2/account":
                return _FakeResponse(account)
            if path == "/v2/positions":
                return _FakeResponse(positions if positions is not None else [])
            symbol = path.rsplit("/", 1)[-1]
            default = {"tradable": True, "fractionable": True, "status": "active"}
            return _FakeResponse((assets or {}).get(symbol, default))

    monkeypatch.setattr(module, "AlpacaPaperBrokerAdapter", _Stub)
    return calls


def _preflight(tmp_path, symbols, *, capital=100000.0):
    from app.cli.mom12_1_portfolio import build_parser, preflight

    args = build_parser().parse_args(
        [
            "--output-dir", str(tmp_path / "out"),
            "preflight",
            "--signal-csv", str(write_signal(tmp_path, symbols)),
            "--provenance", PROVENANCE_TEST_REPLAY,
            "--allocated-capital", str(capital),
            "--no-database",
        ]
    )
    summary = preflight(args)
    # The summary omits the per-symbol book; the file on disk carries it, and
    # that file is the artefact a reviewer would read.
    written = json.loads(
        next((tmp_path / "out").glob("preflight_*.json")).read_text(encoding="utf-8")
    )
    return summary, written


def test_the_preflight_never_reads_the_account_book_as_ownership(tmp_path, monkeypatch):
    """The account holds a name the signal dropped. Without an ownership
    ledger, that is not an exit -- it is an unanswered question."""
    symbols = universe(5)
    _fake_alpaca(
        monkeypatch,
        positions=[{"symbol": "LEGACY", "qty": "40", "market_value": "4000"}],
    )
    result, full = _preflight(tmp_path, symbols)

    assert result["positions"]["count"] == 1        # the book was read
    assert result["positions"]["account_positions_are_not_ownership"] is True
    assert result["ownership"]["available"] is False
    assert full["would_submit"]["exits"] == []      # and nothing was exited
    assert BLOCKER_OWNERSHIP in result["would_submit"]["blockers"]


def test_the_preflight_touches_no_mutating_endpoint(tmp_path, monkeypatch):
    calls = _fake_alpaca(monkeypatch, positions=[])
    result, _ = _preflight(tmp_path, universe(3))

    assert "/v2/account" in calls
    assert "/v2/positions" in calls
    assert all(not path.startswith("/v2/orders") for path in calls)
    assert result["mutating_endpoints_used"] == []
    assert result["orders_submitted"] is False
    assert result["authorizes_paper_or_live"] is False


def test_the_preflight_resolves_every_input_a_rebalance_needs(tmp_path, monkeypatch):
    _fake_alpaca(monkeypatch, positions=[])
    result, _ = _preflight(tmp_path, universe(4))

    # Each of these is a question a live rebalance must answer before it can
    # size anything, so the preflight answers all of them or says it could not.
    assert result["signal"]["strategy"] == STRATEGY_MOM_12_1
    assert result["account"]["buying_power"] == "100000"
    assert result["account"]["allocated_capital"] == "100000.0"
    assert result["positions"]["source"] == "alpaca_fresh_read"
    assert "reconciliation" in result
    assert "ownership" in result
    assert result["asset_preflight"]["selected_count"] == 4
    assert "would_submit" in result and "verification" in result


def test_alpaca_not_the_database_decides_tradability(tmp_path, monkeypatch):
    """A name the database still calls active, which Alpaca will not trade.

    `is_active` is KefTrade housekeeping refreshed on its own schedule; it
    cannot speak for what the broker will accept right now.
    """
    symbols = universe(3)
    _fake_alpaca(
        monkeypatch,
        assets={
            symbols[1]: {"tradable": False, "fractionable": True, "status": "inactive"}
        },
    )
    result, _ = _preflight(tmp_path, symbols)

    assert result["tradability_authority"] == "alpaca"
    assert result["database_is_active_is_not_authoritative"] is True
    assert symbols[1] in result["asset_preflight"]["not_tradable_symbols"]
    assert result["would_submit"]["blocked"] is True


def test_a_non_fractionable_name_blocks_the_whole_preflight(tmp_path, monkeypatch):
    symbols = universe(3)
    _fake_alpaca(
        monkeypatch,
        assets={
            symbols[2]: {"tradable": True, "fractionable": False, "status": "active"}
        },
    )
    result, full = _preflight(tmp_path, symbols)

    assert symbols[2] in result["asset_preflight"]["nonfractionable_symbols"]
    assert result["would_submit"]["blocked"] is True
    # Not dropped, not substituted, not redistributed across the survivors.
    assert full["would_submit"]["signal"]["selected_count"] == 3
    assert len(full["would_submit"]["symbol_plans"]) == 3


def test_a_symbol_alpaca_will_not_describe_fails_closed(tmp_path, monkeypatch):
    symbols = universe(3)
    _fake_alpaca(
        monkeypatch,
        # Tradable, but Alpaca did not say whether it is fractionable.
        assets={symbols[0]: {"tradable": True, "status": "active"}},
    )
    result, _ = _preflight(tmp_path, symbols)

    assert symbols[0] in result["asset_preflight"]["unknown_fractionability_symbols"]
    assert result["would_submit"]["blocked"] is True


def test_capital_beyond_buying_power_is_refused_not_reduced(tmp_path, monkeypatch):
    _fake_alpaca(
        monkeypatch,
        account={"id": "a", "status": "ACTIVE", "buying_power": "5000", "cash": "5000"},
    )
    result, _ = _preflight(tmp_path, universe(3), capital=100000.0)

    assert "ALLOCATED_CAPITAL_EXCEEDS_BUYING_POWER" in result["preflight_blockers"]
    # The request is refused rather than quietly resized to what fits.
    assert result["account"]["allocated_capital"] == "100000.0"


def test_an_unreadable_account_is_a_blocker(tmp_path, monkeypatch):
    import app.brokers.alpaca_paper as module

    class _Broken:
        def __init__(self, *args, **kwargs):
            pass

        async def _get(self, path, label):
            if path == "/v2/account":
                raise RuntimeError("boom")
            return _FakeResponse([] if path == "/v2/positions" else
                                 {"tradable": True, "fractionable": True})

    monkeypatch.setattr(module, "AlpacaPaperBrokerAdapter", _Broken)
    result, _ = _preflight(tmp_path, universe(2))
    assert "ALPACA_ACCOUNT_UNREADABLE" in result["preflight_blockers"]


def test_skipping_the_database_can_never_produce_a_clean_preflight(tmp_path, monkeypatch):
    """Ownership and reconciliation live in KefTrade, so a run that skips it
    says so rather than looking clean."""
    _fake_alpaca(monkeypatch, positions=[])
    result, _ = _preflight(tmp_path, universe(3))

    assert "DATABASE_NOT_CONSULTED" in result["preflight_blockers"]
    assert result["reconciliation"] is None
    assert result["ownership"]["available"] is False


def test_the_preflight_picks_the_latest_run_not_the_latest_clean_one():
    """Searching for the latest *clean* run finds one however old, and however
    many failures came after it. That is choosing the evidence that permits the
    trade."""
    import inspect

    from app.cli import mom12_1_portfolio

    sql = inspect.getsource(mom12_1_portfolio._latest_reconciliation)
    assert "completed_at IS NOT NULL" in sql
    assert "ORDER BY completed_at DESC" in sql
    assert "status = 'clean'" not in sql
    assert "status='clean'" not in sql


def test_the_preflight_reads_ownership_from_its_own_table():
    import inspect

    from app.cli import mom12_1_portfolio

    source = inspect.getsource(mom12_1_portfolio._ownership_ledger)
    assert "strategy_owned_positions" in source
    assert "broker_positions" not in source  # never the account book
    assert "unavailable" in source           # unreadable means blocked


def test_the_ownership_table_exists_in_the_migration():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sql = (root / "database" / "migrations" / "081_fractional_execution.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS strategy_owned_positions" in sql
    # A negative attribution would be a short by bookkeeping.
    assert "strategy_owned_positions_non_negative_check CHECK (quantity >= 0)" in sql
    assert "UNIQUE (strategy, broker_account_id, symbol)" in sql


# ---------------------------------------------------------------------------
# Selected symbols: the strategy's allocation, not the account's
# ---------------------------------------------------------------------------
#
# The exit path was ownership-driven from the previous commit. The rebalance
# path was not: it measured every selected name's current allocation from the
# account book, so another strategy's shares counted as this strategy's.


def _shared_account_plan(tmp_path, *, mom_owns, account_holds, price=100):
    """One selected symbol, held by the account, partly owned by MOM."""
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    target_symbol = symbols[0]
    book = {
        target_symbol: ConfirmedPosition(
            symbol=target_symbol,
            quantity=Decimal(str(account_holds)),
            market_value=Decimal(str(account_holds)) * Decimal(str(price)),
            observed_at=NOW - timedelta(seconds=30),
        )
    }
    plan = build_rebalance_plan(
        signal=signal,
        # $100k over ten names is $10,000 each, which at $100 is 100 shares.
        # Scaled below by the caller's choice of capital.
        allocated_capital=CAPITAL,
        reference_prices={s: Decimal(str(price)) for s in symbols},
        asset_facts=facts(symbols),
        positions=book,
        ownership=ledger({target_symbol: mom_owns}),
        reconciliation=evidence(),
    )
    return target_symbol, next(p for p in plan.symbol_plans if p.symbol == target_symbol), plan


def test_another_strategys_shares_are_not_this_strategys_position(tmp_path):
    """The mandated case.

    The account holds 100 AAPL. MOM owns 20 of them; the other 80 are someone
    else's. MOM's target is 30 shares' worth. MOM must plan from 20 to 30 --
    a buy of 10 shares -- and must not read the account's 100 as its own.
    """
    # $30,000 over ten names is $3,000 each: 30 shares at $100.
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    aapl = symbols[0]
    account = {
        aapl: ConfirmedPosition(
            symbol=aapl, quantity=Decimal(100), market_value=Decimal(10000),
            observed_at=NOW - timedelta(seconds=30),
        )
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=Decimal(30000),
        reference_prices={s: Decimal(100) for s in symbols},
        asset_facts=facts(symbols),
        positions=account,
        ownership=ledger({aapl: 20}),   # MOM owns 20; 80 belong to someone else
        reconciliation=evidence(),
    )
    first = next(p for p in plan.symbol_plans if p.symbol == aapl)

    assert first.current_quantity == Decimal(20)        # not 100
    assert first.current_dollars == Decimal(2000)       # not 10000
    assert first.required_dollar_delta == Decimal(1000)  # 20 -> 30 shares
    assert first.action == "buy"
    assert first.order_payload["side"] == "buy"
    assert first.order_payload["notional"] == "1000.00"


def test_the_account_book_never_turns_a_buy_into_a_reduction(tmp_path):
    """The failure this defect produced: MOM owns nothing, the account holds
    $8,000 of the name for someone else, MOM's target is $2,000. Reading the
    account book made MOM $6,000 overweight and it planned a sell."""
    symbols = universe(50)  # $100k / 50 = $2,000 each
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    aapl = symbols[0]
    account = {
        aapl: ConfirmedPosition(
            symbol=aapl, quantity=Decimal(80), market_value=Decimal(8000),
            observed_at=NOW - timedelta(seconds=30),
        )
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices={s: Decimal(100) for s in symbols},
        asset_facts=facts(symbols),
        positions=account,
        ownership=ledger({}),           # MOM owns none of it
        reconciliation=evidence(),
    )
    first = next(p for p in plan.symbol_plans if p.symbol == aapl)

    assert first.current_quantity == Decimal(0)
    assert first.current_dollars == Decimal(0)
    assert first.required_dollar_delta == Decimal(2000)
    assert first.action == "buy"                      # not "reduce", not blocked
    assert first.order_payload["notional"] == "2000.00"


def test_a_reduction_is_measured_against_the_owned_slice(tmp_path):
    """The inverse case. MOM owns 40 of the account's 100; the target is 30.
    MOM may reduce by 10 -- never by 70, which is what measuring against the
    account's 100 would produce."""
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    aapl = symbols[0]
    account = {
        aapl: ConfirmedPosition(
            symbol=aapl, quantity=Decimal(100), market_value=Decimal(10000),
            observed_at=NOW - timedelta(seconds=30),
        )
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=Decimal(30000),  # $3,000 = 30 shares
        reference_prices={s: Decimal(100) for s in symbols},
        asset_facts=facts(symbols),
        positions=account,
        ownership=ledger({aapl: 40}),
        reconciliation=evidence(),
    )
    first = next(p for p in plan.symbol_plans if p.symbol == aapl)

    assert first.current_quantity == Decimal(40)
    assert first.current_dollars == Decimal(4000)
    assert first.required_dollar_delta == Decimal(-1000)
    assert first.action == "reduce"
    assert first.order_payload["side"] == "sell"
    assert Decimal(first.order_payload["qty"]) == Decimal(10)
    # The other strategy's 60 shares are untouched by construction.
    assert Decimal(first.order_payload["qty"]) < Decimal(40)


def test_attributed_value_is_marked_pro_rata_not_at_reference_price(tmp_path):
    """MOM's slice moves with the same marks as the rest of the position, so a
    stale reference price cannot make the strategy look richer than it is."""
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    aapl = symbols[0]
    account = {
        aapl: ConfirmedPosition(
            # 100 shares marked at $90, while the reference price still says $100.
            symbol=aapl, quantity=Decimal(100), market_value=Decimal(9000),
            observed_at=NOW - timedelta(seconds=30),
        )
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=Decimal(30000),
        reference_prices={s: Decimal(100) for s in symbols},
        asset_facts=facts(symbols),
        positions=account,
        ownership=ledger({aapl: 20}),
        reconciliation=evidence(),
    )
    first = next(p for p in plan.symbol_plans if p.symbol == aapl)
    assert first.current_dollars == Decimal(1800)  # 20/100 of $9,000


def test_ownership_beyond_what_the_account_holds_blocks_the_symbol(tmp_path):
    """KefTrade says MOM owns 50; the broker shows 10 in the entire account.
    One record is wrong and neither may be quietly believed."""
    from app.services.portfolio_execution_bridge import (
        BLOCKER_OWNERSHIP_EXCEEDS_ACCOUNT,
    )

    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    aapl = symbols[0]
    account = {
        aapl: ConfirmedPosition(
            symbol=aapl, quantity=Decimal(10), market_value=Decimal(1000),
            observed_at=NOW - timedelta(seconds=30),
        )
    }
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices={s: Decimal(100) for s in symbols},
        asset_facts=facts(symbols),
        positions=account,
        ownership=ledger({aapl: 50}),
        reconciliation=evidence(),
    )
    first = next(p for p in plan.symbol_plans if p.symbol == aapl)

    assert BLOCKER_OWNERSHIP_EXCEEDS_ACCOUNT in first.blockers
    assert first.order_payload is None
    assert plan.blocked is True


def test_an_unavailable_ledger_blocks_every_selected_symbol_too(tmp_path):
    """Not just exits. Without a ledger, a buy is sized from a guess about what
    the strategy already holds, which is the same defect pointing the other
    way."""
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions={s: position(s, 5) for s in symbols},
        ownership=ledger({}, available=False),
        reconciliation=evidence(),
    )
    assert plan.blocked is True
    assert all(p.order_payload is None for p in plan.symbol_plans)
    assert all(BLOCKER_OWNERSHIP in p.blockers for p in plan.symbol_plans)


def test_an_empty_ledger_still_buys_the_full_target(tmp_path):
    """An available, empty ledger is a conclusion -- the strategy owns nothing
    -- and must not be confused with an unreadable one."""
    symbols = universe(10)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        ownership=ledger({}), reconciliation=evidence(),
    )
    assert plan.blocked is False
    assert all(p.action == "buy" for p in plan.symbol_plans)
    assert all(p.current_quantity == Decimal(0) for p in plan.symbol_plans)


# ---------------------------------------------------------------------------
# Reconciliation freshness is enforced, not merely available
# ---------------------------------------------------------------------------


def test_an_old_but_clean_reconciliation_blocks_the_rebalance(tmp_path):
    """`clean` describes the moment the run finished. Two weeks later it
    describes a book nobody has checked since."""
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions={"OLDCO": position("OLDCO", 25)},
        ownership=ledger({"OLDCO": 25}),
        reconciliation=evidence(age=timedelta(days=14)),  # clean, but ancient
    )
    assert BLOCKER_RECONCILIATION in plan.blockers
    assert plan.blocked is True
    assert all(p.order_payload is None for p in plan.exits)


def test_an_old_clean_reconciliation_blocks_a_reduction_sell():
    from app.services.strategy_ownership import ReconciliationEvidenceMissing

    with pytest.raises(ReconciliationEvidenceMissing, match="beyond the"):
        plan_position_reduction(
            position=position("AAPL", 100), target_dollars=Decimal(3000),
            reference_price=Decimal(100),
            reconciliation=evidence(age=timedelta(days=14)),
            now=NOW,
        )


def test_an_old_clean_reconciliation_blocks_a_full_exit():
    from app.services.strategy_ownership import ReconciliationEvidenceMissing

    with pytest.raises(ReconciliationEvidenceMissing, match="beyond the"):
        plan_full_exit(
            position=position("AAPL", 100),
            reconciliation=evidence(age=timedelta(days=14)),
            now=NOW,
        )


def test_an_old_clean_reconciliation_is_refused_at_the_adapter(monkeypatch):
    """And zero orders are posted."""
    import asyncio

    import httpx

    from app.services.strategy_ownership import ReconciliationEvidenceMissing

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=_position_body("AAPL", 10))
        return httpx.Response(200, json={"id": "must-not-happen"})

    adapter, client = _paper_adapter(monkeypatch, handler)

    async def run() -> None:
        with pytest.raises(ReconciliationEvidenceMissing, match="beyond the"):
            await adapter.submit_order(
                _sell("AAPL", 4),
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(age=timedelta(days=14)),
            )
        await client.aclose()

    asyncio.run(run())
    assert not any("POST" in call for call in seen)


def test_the_freshness_limit_is_configured_and_read_at_call_time(monkeypatch):
    from app.services.strategy_ownership import max_reconciliation_age
    from app.settings import Settings, settings

    assert Settings(_env_file=None).broker_reconciliation_max_age_seconds == 900
    assert max_reconciliation_age() == timedelta(seconds=900)
    monkeypatch.setattr(settings, "broker_reconciliation_max_age_seconds", 60)
    assert max_reconciliation_age() == timedelta(seconds=60)
    # And the tightened limit takes effect without a restart.
    with pytest.raises(Exception, match="beyond the"):
        plan_full_exit(
            position=position("AAPL", 10),
            reconciliation=evidence(age=timedelta(seconds=61)),
            now=NOW,
        )


def test_no_mutation_path_opts_out_of_the_freshness_bound():
    """`max_age=None` disables the bound. It exists for unit-testing the other
    branches in isolation, and no production caller may reach for it."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "require_clean_reconciliation":
                continue
            for keyword in node.keywords:
                explicitly_none = (
                    keyword.arg == "max_age"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is None
                )
                if explicitly_none:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_every_sell_authorising_path_checks_reconciliation():
    """Named individually, because the defect was that the check existed and
    the callers did not use it."""
    import inspect

    from app.brokers import alpaca_paper
    from app.services import portfolio_execution_bridge, position_reducing_sell

    for source in (
        inspect.getsource(portfolio_execution_bridge.build_rebalance_plan),
        inspect.getsource(position_reducing_sell.plan_position_reduction),
        inspect.getsource(position_reducing_sell.plan_full_exit),
        inspect.getsource(position_reducing_sell.revalidate_reduction_against_fresh_positions),
        inspect.getsource(
            alpaca_paper.AlpacaPaperPortfolioAdapter._submit_governed_order
        ),
    ):
        assert "require_clean_reconciliation" in source
