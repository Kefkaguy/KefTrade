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
NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


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
        reconciliation_status="drifted",
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
    )
    assert plan.blocked is True


# ---------------------------------------------------------------------------
# Position-reducing sells -- crossing zero must be impossible
# ---------------------------------------------------------------------------


def test_a_reduction_leaves_a_non_negative_position():
    order = plan_position_reduction(
        position=position("AAPL", 100), target_dollars=Decimal(3000),
        reference_price=Decimal(100), now=NOW,
    )
    assert order.sell_qty == Decimal(70)
    assert order.resulting_quantity == Decimal(30)
    assert order.closes_position is False


def test_a_full_exit_sells_exactly_the_confirmed_quantity():
    order = plan_full_exit(position=position("AAPL", 37.5), now=NOW)
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
            reference_price=Decimal(100), now=NOW,
        )


def test_there_is_no_position_to_reduce():
    with pytest.raises(ShortSellProhibited, match="no confirmed long position"):
        plan_position_reduction(
            position=None, target_dollars=Decimal(0),
            reference_price=Decimal(100), now=NOW,
        )
    with pytest.raises(ShortSellProhibited, match="no confirmed long position"):
        plan_full_exit(position=None, now=NOW)


def test_a_non_long_position_cannot_be_reduced():
    for quantity in (0, -5):
        with pytest.raises(ShortSellProhibited, match="not a long position|holds"):
            plan_position_reduction(
                position=position("AAPL", quantity), target_dollars=Decimal(0),
                reference_price=Decimal(100), now=NOW,
            )


def test_a_stale_position_snapshot_is_refused():
    """Selling against a remembered position is how an account ends up short."""
    stale = position("AAPL", 100, age=max_position_staleness() + timedelta(seconds=1))
    with pytest.raises(ShortSellProhibited, match="stale|older than"):
        plan_position_reduction(
            position=stale, target_dollars=Decimal(3000),
            reference_price=Decimal(100), now=NOW,
        )
    with pytest.raises(ShortSellProhibited, match="stale"):
        plan_full_exit(position=stale, now=NOW)


def test_a_fresh_snapshot_at_the_boundary_is_accepted():
    boundary = position("AAPL", 100, age=max_position_staleness())
    order = plan_position_reduction(
        position=boundary, target_dollars=Decimal(3000),
        reference_price=Decimal(100), now=NOW,
    )
    assert order.sell_qty == Decimal(70)


def test_a_dirty_reconciliation_blocks_a_sell():
    dirty = position("AAPL", 100, status="drifted")
    with pytest.raises(ShortSellProhibited, match="not clean|reconciliation"):
        plan_position_reduction(
            position=dirty, target_dollars=Decimal(3000),
            reference_price=Decimal(100), now=NOW,
        )


def test_a_target_at_or_above_the_holding_is_not_a_reduction():
    with pytest.raises(FractionalExecutionError, match="not a reduction"):
        plan_position_reduction(
            position=position("AAPL", 10), target_dollars=Decimal(1000),
            reference_price=Decimal(100), now=NOW,
        )


def test_a_naive_timestamp_is_refused():
    naive = ConfirmedPosition(
        symbol="AAPL", quantity=Decimal(10), market_value=Decimal(1000),
        observed_at=datetime(2026, 9, 1, 14, 30),  # noqa: DTZ001 -- the point
        reconciliation_status="clean",
    )
    with pytest.raises(FractionalExecutionError, match="carries no timezone"):
        plan_full_exit(position=naive, now=NOW)


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
    """Even with both flags on and a live client, a sell that does not name the
    positions it reduces cannot be shown to be safe, so it is refused."""
    import asyncio

    import httpx

    from app.brokers.alpaca_paper import AlpacaPaperBrokerAdapter
    from app.brokers.base import BrokerMutationDisabled
    from app.settings import settings

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "should-never-happen"})

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
    adapter = AlpacaPaperBrokerAdapter(client=client)

    async def run() -> None:
        with pytest.raises(BrokerMutationDisabled, match="requires the confirmed long"):
            await adapter.submit_order({"symbol": "AAPL", "side": "sell", "qty": "1"})
        # A sell naming a position it would overshoot is refused too.
        with pytest.raises(ShortSellProhibited, match="would open a short"):
            await adapter.submit_order(
                {"symbol": "AAPL", "side": "sell", "qty": "999"},
                confirmed_positions={"AAPL": position("AAPL", 10)},
            )
        await client.aclose()

    asyncio.run(run())
    assert requests == []  # nothing reached the network


def test_the_adapter_still_names_buy_and_reducing_sells_only():
    import app.brokers.alpaca_paper as module

    source = inspect.getsource(module.AlpacaPaperBrokerAdapter.submit_order)
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
    symbols = universe(5)
    signal = load_portfolio_signal(
        write_signal(tmp_path, symbols), provenance=PROVENANCE_TEST_REPLAY
    )
    held = {"OLDCO": position("OLDCO", 25, status="drifted")}
    plan = build_rebalance_plan(
        signal=signal, allocated_capital=CAPITAL,
        reference_prices=prices(symbols), asset_facts=facts(symbols),
        positions=held, reconciliation_status="clean",
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
