"""How attribution is created and destroyed.

The invariant under test throughout: only a confirmed fill moves ownership, it
moves it by exactly the confirmed quantity, and applying the same fill twice
does nothing the second time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.strategy_ownership_lifecycle import (
    APPLIED,
    DUPLICATE,
    FAULT_SELL_EXCEEDS_ATTRIBUTION,
    NON_FILL_ORDER_STATUSES,
    REJECTED,
    UNATTRIBUTED,
    AttributedPosition,
    ConfirmedFill,
    OrderAttribution,
    OwnershipLifecycleError,
    OwnershipState,
    apply_confirmed_fill,
    apply_confirmed_fills,
    attributions_from_rows,
    fills_from_rows,
    ownership_change_for_order_status,
    ownership_rows,
    replay_fills,
    with_reconciliation_provenance,
)

MOM = "MOM_12_1"
OTHER = "MEANREV_5"
ACCOUNT = 7
T0 = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def fill(
    fill_id,
    symbol="AAPL",
    side="buy",
    qty="10",
    price="100",
    *,
    client_order_id="kt-mom-1",
    account=ACCOUNT,
    at=None,
    activity_type="fill",
):
    return ConfirmedFill(
        fill_id=fill_id,
        broker_account_id=account,
        broker_order_id=f"broker-{fill_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        transaction_at=at or T0,
        activity_type=activity_type,
    )


def attribution(client_order_id="kt-mom-1", *, strategy=MOM, symbol="AAPL", account=ACCOUNT):
    return OrderAttribution(
        client_order_id=client_order_id,
        strategy=strategy,
        strategy_version="1.0.0",
        broker_account_id=account,
        symbol=symbol,
    )


def state(**positions):
    st = OwnershipState(
        strategy=MOM, strategy_version="1.0.0", broker_account_id=ACCOUNT
    )
    for symbol, qty in positions.items():
        st.positions[symbol] = AttributedPosition(
            strategy=MOM, strategy_version="1.0.0", broker_account_id=ACCOUNT,
            symbol=symbol, quantity=Decimal(str(qty)),
            average_entry_price=Decimal(100), as_of=T0 - timedelta(days=1),
        )
    return st


# ---------------------------------------------------------------------------
# Buys and sells
# ---------------------------------------------------------------------------


def test_a_confirmed_buy_fill_increases_attribution():
    st = state()
    result = apply_confirmed_fill(fill("f1"), attribution=attribution(), state=st)

    assert result.status == APPLIED
    assert result.quantity_delta == Decimal(10)
    assert st.owned_quantity("AAPL") == Decimal(10)
    assert result.changed_ownership is True


def test_a_confirmed_sell_fill_decreases_attribution():
    st = state(AAPL=25)
    result = apply_confirmed_fill(
        fill("f1", side="sell", qty="10"), attribution=attribution(), state=st
    )

    assert result.status == APPLIED
    assert result.quantity_delta == Decimal(-10)
    assert st.owned_quantity("AAPL") == Decimal(15)


def test_a_partial_buy_fill_credits_only_what_filled():
    """An order for 100 that filled 30 makes the strategy own 30, not 100."""
    st = state()
    result = apply_confirmed_fill(
        fill("f1", qty="30", activity_type="partial_fill"),
        attribution=attribution(),
        state=st,
    )
    assert result.status == APPLIED
    assert st.owned_quantity("AAPL") == Decimal(30)


def test_successive_partial_fills_accumulate_to_the_filled_total():
    st = state()
    apply_confirmed_fills(
        [
            fill("f1", qty="30", activity_type="partial_fill", at=T0),
            fill("f2", qty="45", activity_type="partial_fill", at=T0 + timedelta(seconds=1)),
            fill("f3", qty="25", activity_type="fill", at=T0 + timedelta(seconds=2)),
        ],
        attributions={"kt-mom-1": attribution()},
        state=st,
    )
    assert st.owned_quantity("AAPL") == Decimal(100)


def test_a_partial_sell_fill_debits_only_what_filled():
    st = state(AAPL=100)
    apply_confirmed_fill(
        fill("f1", side="sell", qty="40", activity_type="partial_fill"),
        attribution=attribution(),
        state=st,
    )
    assert st.owned_quantity("AAPL") == Decimal(60)


def test_fractional_quantities_survive_the_round_trip():
    st = state()
    apply_confirmed_fill(
        fill("f1", qty="1.234567891"), attribution=attribution(), state=st
    )
    assert st.owned_quantity("AAPL") == Decimal("1.234567891")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_replaying_the_same_fill_changes_nothing():
    st = state()
    first = apply_confirmed_fill(fill("f1"), attribution=attribution(), state=st)
    second = apply_confirmed_fill(fill("f1"), attribution=attribution(), state=st)

    assert first.status == APPLIED
    assert second.status == DUPLICATE
    assert second.quantity_delta == Decimal(0)
    assert st.owned_quantity("AAPL") == Decimal(10)  # not 20


def test_a_duplicated_activity_page_does_not_double_count():
    """The realistic shape: the broker returns an overlapping page."""
    st = state()
    page_one = [fill("f1", at=T0), fill("f2", at=T0 + timedelta(seconds=1))]
    page_two = [fill("f2", at=T0 + timedelta(seconds=1)), fill("f3", at=T0 + timedelta(seconds=2))]
    attributions = {"kt-mom-1": attribution()}

    apply_confirmed_fills(page_one, attributions=attributions, state=st)
    results = apply_confirmed_fills(page_two, attributions=attributions, state=st)

    assert [r.status for r in results] == [DUPLICATE, APPLIED]
    assert st.owned_quantity("AAPL") == Decimal(30)  # three fills of 10


def test_the_idempotency_key_is_the_brokers_own_id():
    """Not one we derive. A derived key changes when the derivation changes,
    and then every historical fill applies again."""
    st = state()
    apply_confirmed_fill(fill("activity-42"), attribution=attribution(), state=st)
    assert "activity-42" in st.applied_fill_ids

    # Same execution, re-fetched with different surrounding detail.
    same = ConfirmedFill(
        fill_id="activity-42",
        broker_account_id=ACCOUNT,
        broker_order_id="broker-activity-42",
        client_order_id="kt-mom-1",
        symbol="AAPL",
        side="buy",
        quantity=Decimal(10),
        price=Decimal("100.01"),          # a different mark
        transaction_at=T0 + timedelta(minutes=5),  # re-stamped
        activity_type="fill",
    )
    assert apply_confirmed_fill(same, attribution=attribution(), state=st).status == DUPLICATE
    assert st.owned_quantity("AAPL") == Decimal(10)


def test_a_fill_without_a_durable_id_is_refused_outright():
    with pytest.raises(OwnershipLifecycleError, match="durable key"):
        fill("")


def test_replay_reproduces_the_incremental_ledger_exactly():
    """Restart safety. Whatever the incremental path built, replaying every
    fill from the beginning must build the same thing."""
    attributions = {"kt-mom-1": attribution(), "kt-mom-2": attribution("kt-mom-2", symbol="MSFT")}
    history = [
        fill("f1", qty="10", at=T0),
        fill("f2", symbol="MSFT", qty="5", client_order_id="kt-mom-2", at=T0 + timedelta(seconds=1)),
        fill("f3", side="sell", qty="4", at=T0 + timedelta(seconds=2)),
        fill("f4", qty="2.5", at=T0 + timedelta(seconds=3)),
    ]

    incremental = state()
    for one in history:
        apply_confirmed_fill(
            one, attribution=attributions.get(one.client_order_id), state=incremental
        )

    replayed = replay_fills(
        history, attributions=attributions, strategy=MOM,
        strategy_version="1.0.0", broker_account_id=ACCOUNT,
    )
    assert ownership_rows(incremental) == ownership_rows(replayed)
    assert incremental.applied_fill_ids == replayed.applied_fill_ids


def test_replay_does_not_depend_on_the_order_activities_arrive_in():
    attributions = {"kt-mom-1": attribution()}
    history = [
        fill("f1", qty="10", at=T0),
        fill("f2", side="sell", qty="4", at=T0 + timedelta(seconds=1)),
        fill("f3", qty="7", at=T0 + timedelta(seconds=2)),
    ]
    forwards = replay_fills(
        history, attributions=attributions, strategy=MOM,
        strategy_version="1.0.0", broker_account_id=ACCOUNT,
    )
    backwards = replay_fills(
        list(reversed(history)), attributions=attributions, strategy=MOM,
        strategy_version="1.0.0", broker_account_id=ACCOUNT,
    )
    assert ownership_rows(forwards) == ownership_rows(backwards)
    assert forwards.owned_quantity("AAPL") == Decimal(13)


# ---------------------------------------------------------------------------
# Shared accounts
# ---------------------------------------------------------------------------


def test_another_strategys_fill_never_moves_this_strategys_attribution():
    st = state(AAPL=20)
    result = apply_confirmed_fill(
        fill("f1", qty="80", client_order_id="kt-other-1"),
        attribution=attribution("kt-other-1", strategy=OTHER),
        state=st,
    )
    assert result.status == UNATTRIBUTED
    assert st.owned_quantity("AAPL") == Decimal(20)  # unchanged


def test_a_sell_fill_never_subtracts_another_strategys_attribution():
    """The account sells 80 AAPL for another strategy while MOM holds 20."""
    st = state(AAPL=20)
    apply_confirmed_fill(
        fill("f1", side="sell", qty="80", client_order_id="kt-other-1"),
        attribution=attribution("kt-other-1", strategy=OTHER),
        state=st,
    )
    assert st.owned_quantity("AAPL") == Decimal(20)
    assert st.has_faults is False  # not this strategy's problem, not a fault


def test_an_unclaimed_manual_fill_moves_nothing():
    st = state(AAPL=20)
    result = apply_confirmed_fill(
        fill("f1", client_order_id="typed-by-hand"), attribution=None, state=st
    )
    assert result.status == UNATTRIBUTED
    assert result.strategy is None
    assert st.owned_quantity("AAPL") == Decimal(20)


def test_two_strategies_in_one_symbol_keep_separate_books():
    attributions = {
        "kt-mom-1": attribution(),
        "kt-other-1": attribution("kt-other-1", strategy=OTHER),
    }
    history = [
        fill("f1", qty="20", client_order_id="kt-mom-1", at=T0),
        fill("f2", qty="80", client_order_id="kt-other-1", at=T0 + timedelta(seconds=1)),
    ]
    mom = replay_fills(
        history, attributions=attributions, strategy=MOM,
        strategy_version="1.0.0", broker_account_id=ACCOUNT,
    )
    other = replay_fills(
        history, attributions=attributions, strategy=OTHER,
        strategy_version="1.0.0", broker_account_id=ACCOUNT,
    )
    assert mom.owned_quantity("AAPL") == Decimal(20)
    assert other.owned_quantity("AAPL") == Decimal(80)
    # The account holds 100; neither strategy may claim it.
    assert mom.owned_quantity("AAPL") + other.owned_quantity("AAPL") == Decimal(100)


# ---------------------------------------------------------------------------
# Attribution can never go negative
# ---------------------------------------------------------------------------


def test_a_sell_beyond_attribution_is_rejected_not_clamped():
    st = state(AAPL=20)
    result = apply_confirmed_fill(
        fill("f1", side="sell", qty="50"), attribution=attribution(), state=st
    )
    assert result.status == REJECTED
    assert result.reason == FAULT_SELL_EXCEEDS_ATTRIBUTION
    assert st.owned_quantity("AAPL") == Decimal(20)  # not 0, not -30


def test_a_faulted_state_yields_an_unavailable_ledger():
    """A book we know to be wrong is not a book to sell from."""
    st = state(AAPL=20)
    apply_confirmed_fill(
        fill("f1", side="sell", qty="50"), attribution=attribution(), state=st
    )
    ledger = st.to_ledger()
    assert st.has_faults is True
    assert ledger.available is False
    assert ledger.source == "faulted"


def test_a_clean_state_yields_an_available_ledger():
    st = state(AAPL=20)
    ledger = st.to_ledger()
    assert ledger.available is True
    assert ledger.owned_quantity("AAPL") == Decimal(20)
    assert ledger.held_symbols == ("AAPL",)


def test_selling_the_whole_attributed_position_is_allowed():
    st = state(AAPL=20)
    result = apply_confirmed_fill(
        fill("f1", side="sell", qty="20"), attribution=attribution(), state=st
    )
    assert result.status == APPLIED
    assert st.owned_quantity("AAPL") == Decimal(0)
    assert st.to_ledger().held_symbols == ()  # zero is not held


def test_a_symbol_mismatch_between_fill_and_attribution_is_a_fault():
    st = state()
    result = apply_confirmed_fill(
        fill("f1", symbol="TSLA"), attribution=attribution(symbol="AAPL"), state=st
    )
    assert result.status == REJECTED
    assert st.has_faults is True


def test_an_account_mismatch_is_a_fault():
    st = state()
    result = apply_confirmed_fill(
        fill("f1", account=99), attribution=attribution(account=99), state=st
    )
    assert result.status == REJECTED
    assert st.has_faults is True


# ---------------------------------------------------------------------------
# Intent is never ownership evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(NON_FILL_ORDER_STATUSES))
def test_no_order_status_moves_ownership(status):
    assert ownership_change_for_order_status(status) == Decimal(0)


def test_a_cancelled_order_leaves_attribution_untouched():
    st = state(AAPL=20)
    for status in ("canceled", "rejected", "expired", "new", "accepted"):
        assert ownership_change_for_order_status(status) == Decimal(0)
    assert st.owned_quantity("AAPL") == Decimal(20)


def test_a_fill_cannot_be_smuggled_through_the_order_status_path():
    with pytest.raises(OwnershipLifecycleError, match="idempotency key"):
        ownership_change_for_order_status("fill")


def test_an_event_that_is_not_a_confirmed_fill_is_rejected():
    st = state()
    result = apply_confirmed_fill(
        fill("f1", activity_type="canceled"), attribution=attribution(), state=st
    )
    assert result.status == REJECTED
    assert st.owned_quantity("AAPL") == Decimal(0)


def test_attribution_carries_no_quantity():
    """The structural guarantee. If OrderAttribution had a quantity field,
    submitted size could become ownership evidence by accident."""
    assert not any(
        "quantity" in name for name in OrderAttribution.__dataclass_fields__
    )


# ---------------------------------------------------------------------------
# Cost basis and provenance
# ---------------------------------------------------------------------------


def test_the_average_entry_price_is_weighted_across_buys():
    st = state()
    attributions = {"kt-mom-1": attribution()}
    apply_confirmed_fills(
        [
            fill("f1", qty="10", price="100", at=T0),
            fill("f2", qty="30", price="200", at=T0 + timedelta(seconds=1)),
        ],
        attributions=attributions,
        state=st,
    )
    # (10*100 + 30*200) / 40 = 175
    assert st.positions["AAPL"].average_entry_price == Decimal(175)


def test_a_sell_does_not_change_what_the_remaining_shares_cost():
    st = state()
    attributions = {"kt-mom-1": attribution()}
    apply_confirmed_fills(
        [
            fill("f1", qty="10", price="100", at=T0),
            fill("f2", side="sell", qty="4", price="500", at=T0 + timedelta(seconds=1)),
        ],
        attributions=attributions,
        state=st,
    )
    assert st.positions["AAPL"].average_entry_price == Decimal(100)
    assert st.owned_quantity("AAPL") == Decimal(6)


def test_as_of_tracks_the_execution_not_the_ingestion():
    st = state()
    late = T0 + timedelta(hours=3)
    apply_confirmed_fill(fill("f1", at=late), attribution=attribution(), state=st)
    assert st.positions["AAPL"].as_of == late


def test_as_of_never_moves_backwards_on_a_late_arriving_fill():
    st = state()
    attributions = {"kt-mom-1": attribution()}
    apply_confirmed_fills(
        [fill("f1", at=T0 + timedelta(hours=3)), fill("f2", at=T0)],
        attributions=attributions,
        state=st,
    )
    assert st.positions["AAPL"].as_of == T0 + timedelta(hours=3)


def test_reconciliation_provenance_records_agreement_without_moving_shares():
    st = state(AAPL=20)
    before = st.owned_quantity("AAPL")
    with_reconciliation_provenance(st, run_id=4242)
    assert st.positions["AAPL"].reconciliation_run_id == 4242
    assert st.owned_quantity("AAPL") == before


def test_the_strategy_version_comes_from_the_attribution():
    st = state()
    apply_confirmed_fill(fill("f1"), attribution=attribution(), state=st)
    assert st.positions["AAPL"].strategy_version == "1.0.0"


# ---------------------------------------------------------------------------
# Row adapters
# ---------------------------------------------------------------------------


def test_fills_are_built_from_broker_fills_rows():
    built = fills_from_rows(
        [
            {
                "broker_activity_id": "act-1",
                "broker_account_id": ACCOUNT,
                "broker_order_id": "ord-1",
                "client_order_id": "kt-mom-1",
                "symbol": "aapl",
                "side": "BUY",
                "quantity": "1.5",
                "price": "101.25",
                "transaction_at": T0,
            }
        ]
    )
    assert built[0].fill_id == "act-1"          # the table's unique key
    assert built[0].symbol == "AAPL"
    assert built[0].quantity == Decimal("1.5")


def test_attributions_are_built_from_their_own_table():
    built = attributions_from_rows(
        [
            {
                "client_order_id": "kt-mom-1",
                "strategy": MOM,
                "strategy_version": "1.0.0",
                "broker_account_id": ACCOUNT,
                "symbol": "aapl",
            }
        ]
    )
    assert built["kt-mom-1"].strategy == MOM
    assert built["kt-mom-1"].symbol == "AAPL"


def test_ownership_rows_are_ready_to_upsert():
    st = state(AAPL=20, MSFT=5)
    rows = ownership_rows(st)
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]  # deterministic order
    assert rows[0]["source"] == "confirmed_fill"
    assert set(rows[0]) == {
        "strategy", "strategy_version", "broker_account_id", "symbol",
        "quantity", "average_entry_price", "as_of", "reconciliation_run_id",
        "source",
    }


def test_the_lifecycle_module_cannot_reach_the_broker():
    """It computes attribution. It does not talk to anyone."""
    import ast
    import inspect

    from app.services import strategy_ownership_lifecycle

    tree = ast.parse(inspect.getsource(strategy_ownership_lifecycle))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("httpx", "app.brokers.alpaca_paper", "app.db"):
        assert banned not in imported


def test_the_lifecycle_tables_exist_in_the_migration():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sql = (root / "database" / "migrations" / "081_fractional_execution.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS strategy_order_attributions" in sql
    assert "CREATE TABLE IF NOT EXISTS strategy_ownership_events" in sql
    # The durable idempotency key.
    assert "UNIQUE (broker_account_id, fill_id)" in sql
    # Attribution carries no quantity column. Comments are stripped first, so
    # the check reads the columns rather than the prose explaining them.
    ddl = sql[sql.index("CREATE TABLE IF NOT EXISTS strategy_order_attributions") :]
    ddl = ddl[: ddl.index(");")]
    columns = [
        line for line in ddl.splitlines() if not line.strip().startswith("--")
    ]
    assert not any("quantity" in line for line in columns)


# ---------------------------------------------------------------------------
# Plan-time attribution closes the loop
# ---------------------------------------------------------------------------


def _tiny_plan(tmp_path):
    """A three-name rebalance, so there are real client order ids to attribute."""
    import csv

    from app.services.fractional_execution import AssetFact
    from app.services.portfolio_execution_bridge import (
        PROVENANCE_TEST_REPLAY,
        build_rebalance_plan,
        load_portfolio_signal,
    )
    from app.services.strategy_ownership import (
        ReconciliationEvidence,
        StrategyOwnershipLedger,
    )

    symbols = [f"SYM{i:04d}" for i in range(3)]
    path = tmp_path / "signal.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["symbol", "weight", "signal_date", "intended_execution_date",
             "strategy", "strategy_version", "universe_hash", "selected_count"]
        )
        for symbol in symbols:
            writer.writerow(
                [symbol, "0.333333333", "2026-07-31", "2026-08-03", MOM,
                 "1.0.0", "f7b50c2b0c0882df", len(symbols)]
            )
    signal = load_portfolio_signal(path, provenance=PROVENANCE_TEST_REPLAY)
    return build_rebalance_plan(
        signal=signal,
        allocated_capital=Decimal(30000),
        reference_prices=dict.fromkeys(symbols, Decimal(100)),
        asset_facts={
            s: AssetFact(symbol=s, tradable=True, fractionable=True) for s in symbols
        },
        ownership=StrategyOwnershipLedger(
            strategy=MOM, positions={}, available=True, source="test"
        ),
        reconciliation=ReconciliationEvidence(
            run_id=1, status="clean",
            completed_at=datetime.now(UTC) - timedelta(minutes=1),
            broker_account_id=ACCOUNT,
        ),
    )


def test_a_plan_produces_one_attribution_per_order(tmp_path):
    from app.services.strategy_ownership_lifecycle import attributions_for_plan

    plan = _tiny_plan(tmp_path)
    rows = attributions_for_plan(plan, broker_account_id=ACCOUNT)

    assert len(rows) == 3
    assert {r["strategy"] for r in rows} == {MOM}
    assert {r["intended_side"] for r in rows} == {"buy"}
    assert all(r["client_order_id"].startswith("kt-mom_12_1-") for r in rows)


def test_plan_attributions_carry_no_quantity(tmp_path):
    """The structural guarantee again, at the row level: nothing here can be
    mistaken for how much the market filled."""
    from app.services.strategy_ownership_lifecycle import attributions_for_plan

    rows = attributions_for_plan(_tiny_plan(tmp_path), broker_account_id=ACCOUNT)
    for row in rows:
        assert not any("quantity" in key or "notional" in key for key in row)


def test_a_replanned_rebalance_attributes_the_same_orders(tmp_path):
    """Client order ids are deterministic, so a retry re-attributes rather than
    claiming a second set of orders."""
    from app.services.strategy_ownership_lifecycle import attributions_for_plan

    first = attributions_for_plan(_tiny_plan(tmp_path), broker_account_id=ACCOUNT)
    again = attributions_for_plan(_tiny_plan(tmp_path), broker_account_id=ACCOUNT)
    assert [r["client_order_id"] for r in first] == [r["client_order_id"] for r in again]


def test_blocked_symbols_are_not_attributed(tmp_path):
    """No payload, no order, nothing to attribute."""
    from app.services.strategy_ownership_lifecycle import attributions_for_plan

    plan = _tiny_plan(tmp_path)
    attributed = {r["client_order_id"] for r in attributions_for_plan(plan, broker_account_id=ACCOUNT)}
    for symbol_plan in plan.symbol_plans:
        if symbol_plan.order_payload is None:
            assert symbol_plan.client_order_id not in attributed


def test_attribution_then_fills_reproduce_the_planned_position(tmp_path):
    """End to end, without a broker: plan -> attribution -> confirmed fills ->
    the ledger the next rebalance will read."""
    from app.services.strategy_ownership_lifecycle import (
        attributions_for_plan,
        attributions_from_rows,
        replay_fills,
    )

    plan = _tiny_plan(tmp_path)
    rows = attributions_for_plan(plan, broker_account_id=ACCOUNT)
    attributions = attributions_from_rows(
        [{**row, "strategy_version": "1.0.0"} for row in rows]
    )

    # $10,000 per name at $100 is 100 shares, arriving as two partial fills.
    confirmed = []
    for index, row in enumerate(rows):
        confirmed.append(
            fill(f"a{index}-1", symbol=row["symbol"], qty="60",
                 client_order_id=row["client_order_id"],
                 activity_type="partial_fill", at=T0 + timedelta(seconds=index))
        )
        confirmed.append(
            fill(f"a{index}-2", symbol=row["symbol"], qty="40",
                 client_order_id=row["client_order_id"],
                 at=T0 + timedelta(seconds=index, milliseconds=500))
        )

    state = replay_fills(
        confirmed, attributions=attributions, strategy=MOM,
        strategy_version="1.0.0", broker_account_id=ACCOUNT,
    )
    ledger = state.to_ledger()
    assert len(ledger.held_symbols) == 3
    assert all(state.owned_quantity(r["symbol"]) == Decimal(100) for r in rows)
    # And the next rebalance would see exactly these as MOM's, nothing else.
    assert set(ledger.held_symbols) == {r["symbol"] for r in rows}
