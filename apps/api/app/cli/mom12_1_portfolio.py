"""MOM_12_1 portfolio bridge CLI -- observe-only throughout.

``plan`` turns a frozen signal CSV into a complete rebalance plan and proves it
reproduces the signal, using whatever local evidence exists.

``ownership`` replays confirmed fills into the attributed book and reports
drift from the stored ledger, writing nothing.

``preflight`` is the account-aware version: it resolves every input a real
rebalance would need -- the frozen signal, Alpaca's own tradable/fractionable
verdict, the paper account, a fresh position read, the latest clean KefTrade
reconciliation, the strategy's own ownership ledger, and buying power -- and
produces the complete book of orders that *would* be submitted, without
submitting any of them.

Neither submits an order. There is no ``run`` verb here, because submission
still belongs to the existing gated execution path.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.fractional_execution import AssetFact
from app.services.portfolio_execution_bridge import (
    MOM_12_1_SHARE_POLICY,
    PROVENANCE_FORWARD,
    PROVENANCE_TEST_REPLAY,
    STRATEGY_MOM_12_1,
    build_rebalance_plan,
    load_portfolio_signal,
    verify_plan_against_signal,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "mom_12_1_portfolio_bridge"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _governance() -> dict[str, Any]:
    from app.settings import settings

    return {
        "observe_only": True,
        "orders_submitted": False,
        "broker_order_submission_enabled": settings.broker_order_submission_enabled,
        "external_paper_execution_enabled": settings.external_paper_execution_enabled,
        "authorizes_paper_or_live": False,
    }


def _asset_facts_from_db(symbols: list[str]) -> dict[str, AssetFact]:
    """Fractionability as the assets table last recorded it.

    A symbol absent from the table, or present with a NULL flag, becomes an
    ``AssetFact`` with ``fractionable=None`` -- unknown, which fails closed at
    the gate rather than being read as "yes".
    """
    from app.cli.stage40_audit import _open_connection
    from app.db import connect

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT symbol, is_active, fractionable
              FROM symbols
             WHERE symbol = ANY(%s)
            """,
            [symbols],
        )
        rows = list(cursor.fetchall() or [])
    return {
        row["symbol"]: AssetFact(
            symbol=row["symbol"],
            tradable=bool(row["is_active"]),
            fractionable=row["fractionable"],
        )
        for row in rows
    }


def _reference_prices(symbols: list[str]) -> dict[str, Decimal]:
    """Latest close per symbol, from the candles already synced locally."""
    from app.cli.stage40_audit import _open_connection
    from app.db import connect

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (symbol) symbol, close
              FROM candles
             WHERE symbol = ANY(%s) AND timeframe = '1d'
             ORDER BY symbol, timestamp DESC
            """,
            [symbols],
        )
        rows = list(cursor.fetchall() or [])
    return {row["symbol"]: Decimal(str(row["close"])) for row in rows}


def plan(args: argparse.Namespace) -> dict[str, Any]:
    """Build and verify one observe-only rebalance plan."""
    signal = load_portfolio_signal(Path(args.signal_csv), provenance=args.provenance)
    symbols = list(signal.symbols)

    if args.offline:
        # Plumbing validation without a database: every name is assumed
        # tradable and fractionable, which is stated in the output so the plan
        # can never be mistaken for a broker-verified one.
        facts = {
            s: AssetFact(symbol=s, tradable=True, fractionable=True) for s in symbols
        }
        prices = {s: Decimal(str(args.assumed_price)) for s in symbols}
    else:
        facts = _asset_facts_from_db(symbols)
        prices = _reference_prices(symbols)

    # No positions, no ownership ledger, no reconciliation evidence: this verb
    # sizes a portfolio from a signal. Any exit it might otherwise infer would
    # rest on none of the evidence an exit requires, so it plans none.
    rebalance = build_rebalance_plan(
        signal=signal,
        allocated_capital=Decimal(str(args.allocated_capital)),
        reference_prices=prices,
        asset_facts=facts,
        share_policy=MOM_12_1_SHARE_POLICY,
    )
    verification = verify_plan_against_signal(rebalance)
    payload = {
        **_governance(),
        "asset_facts_source": "assumed_offline" if args.offline else "database",
        "plan": rebalance.as_dict(),
        "verification": verification,
    }
    _write(Path(args.output_dir) / f"rebalance_{signal.signal_date}.json", payload)
    # The per-symbol detail is large; the caller gets the summary and reads the
    # file for the full book.
    summary = dict(payload)
    summary["plan"] = {
        k: v for k, v in payload["plan"].items() if k not in ("symbol_plans", "exits")
    }
    return summary


def _broker_account() -> dict[str, Any] | None:
    """The KefTrade record of the Alpaca Paper account, if one is registered."""
    from app.cli.stage40_audit import _open_connection
    from app.db import connect

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, external_account_id, account_number_masked, status,
                   last_successful_sync_at
              FROM broker_accounts
             WHERE provider = 'alpaca' AND environment = 'paper'
             ORDER BY id
             LIMIT 1
            """
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def _latest_reconciliation(broker_account_id: int) -> Any:
    """The most recent *completed* reconciliation run for this account.

    Deliberately the latest completed run, not the latest clean one. Searching
    for the latest clean run would find one however old, and however many failed
    runs came after it -- which is choosing the evidence that permits the trade.
    If the newest run is not clean, this returns it anyway, and the bridge
    refuses every sell.
    """
    from app.cli.stage40_audit import _open_connection
    from app.db import connect
    from app.services.strategy_ownership import ReconciliationEvidence

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status, completed_at, broker_account_id
              FROM broker_reconciliation_runs
             WHERE broker_account_id = %s AND completed_at IS NOT NULL
             ORDER BY completed_at DESC
             LIMIT 1
            """,
            [broker_account_id],
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return ReconciliationEvidence(
        run_id=int(row["id"]),
        status=str(row["status"]),
        completed_at=row["completed_at"],
        broker_account_id=int(row["broker_account_id"]),
    )


def _ownership_ledger(strategy: str, broker_account_id: int) -> Any:
    """What this strategy owns, per KefTrade's own attribution.

    Never the Alpaca position book. That book is one book per account and holds
    whatever anyone put there; attributing it to this strategy is exactly the
    error that would let a rebalance liquidate someone else's position.

    A table that cannot be read yields an unavailable ledger, which blocks. An
    empty table yields an available, empty ledger -- the strategy owns nothing,
    so it may sell nothing, which is a conclusion rather than a guess.
    """
    from app.cli.stage40_audit import _open_connection
    from app.db import connect
    from app.services.strategy_ownership import (
        StrategyOwnershipLedger,
        ledger_from_rows,
    )

    try:
        with _open_connection(connect) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT symbol, quantity, as_of
                  FROM strategy_owned_positions
                 WHERE strategy = %s AND broker_account_id = %s AND quantity > 0
                """,
                [strategy, broker_account_id],
            )
            rows = [dict(row) for row in (cursor.fetchall() or [])]
    except Exception:  # noqa: BLE001
        # Migration 081 unapplied, permissions, anything: no ledger is no
        # ownership evidence, and no ownership evidence is no sells.
        return StrategyOwnershipLedger.unavailable(strategy, source="unreadable")
    return ledger_from_rows(rows, strategy=strategy, source="strategy_owned_positions")


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    """The complete would-submit rebalance, resolved from real account state.

    Read-only throughout: ``/v2/account``, ``/v2/positions`` and
    ``/v2/assets/{symbol}`` are the only endpoints touched, and no order is
    built for submission -- only for inspection.

    Every input a live rebalance would consume is resolved here, so that what
    blocks a rebalance is visible before anyone is in a position to submit one.
    """
    import asyncio
    from datetime import UTC, datetime

    from app.brokers.alpaca_paper import AlpacaPaperBrokerAdapter
    from app.services.fractional_execution import preflight_assets
    from app.services.position_reducing_sell import parse_broker_positions

    signal = load_portfolio_signal(Path(args.signal_csv), provenance=args.provenance)
    symbols = list(signal.symbols)
    blockers: list[str] = []

    # --- Alpaca, read-only --------------------------------------------------
    async def fetch() -> dict[str, Any]:
        adapter = AlpacaPaperBrokerAdapter()
        account: dict[str, Any] = {}
        account_error: str | None = None
        try:
            account = (await adapter._get("/v2/account", "account")).payload or {}
        except Exception as error:  # noqa: BLE001
            account_error = error.__class__.__name__

        positions_body: Any = None
        positions_error: str | None = None
        try:
            positions_body = (await adapter._get("/v2/positions", "positions")).payload
        except Exception as error:  # noqa: BLE001
            positions_error = error.__class__.__name__

        assets: dict[str, Any] = {}
        for symbol in symbols:
            try:
                body = (
                    await adapter._get(f"/v2/assets/{symbol}", "asset")
                ).payload or {}
                assets[symbol] = {
                    "tradable": bool(body.get("tradable")),
                    "fractionable": (
                        bool(body["fractionable"]) if "fractionable" in body else None
                    ),
                    "status": body.get("status"),
                }
            except Exception as error:  # noqa: BLE001
                # Deliberately broad, and per symbol: one name Alpaca will not
                # describe must not abort the preflight for the rest. A symbol
                # we could not ask about is not a symbol we may trade, so it
                # fails closed at the gate below rather than here.
                assets[symbol] = {
                    "tradable": None,
                    "fractionable": None,
                    "error": error.__class__.__name__,
                }
        return {
            "account": account,
            "account_error": account_error,
            "positions_body": positions_body,
            "positions_error": positions_error,
            "assets": assets,
        }

    observed = asyncio.run(fetch())
    observed_at = datetime.now(UTC)

    if observed["account_error"] or not observed["account"]:
        blockers.append("ALPACA_ACCOUNT_UNREADABLE")
    if observed["positions_error"] or observed["positions_body"] is None:
        blockers.append("ALPACA_POSITIONS_UNREADABLE")

    # --- Alpaca is authoritative on tradability -----------------------------
    #
    # The database `is_active` flag is a KefTrade housekeeping bit, refreshed on
    # its own schedule. It cannot say whether Alpaca will accept an order right
    # now, so it is recorded as context and never consulted as evidence.
    facts = {
        symbol: AssetFact(
            symbol=symbol,
            tradable=bool(fact.get("tradable")),
            fractionable=fact.get("fractionable"),
        )
        for symbol, fact in observed["assets"].items()
    }
    asset_gate = preflight_assets(symbols, facts, share_policy=MOM_12_1_SHARE_POLICY)

    database_is_active = {}
    disagreements: list[dict[str, Any]] = []
    if not args.no_database:
        try:
            database_is_active = {
                symbol: fact.tradable
                for symbol, fact in _asset_facts_from_db(symbols).items()
            }
        except Exception:  # noqa: BLE001
            database_is_active = {}
        for symbol, db_active in database_is_active.items():
            live = observed["assets"].get(symbol, {}).get("tradable")
            if live is not db_active:
                disagreements.append(
                    {"symbol": symbol, "database_is_active": db_active, "alpaca_tradable": live}
                )

    # --- account-scoped evidence -------------------------------------------
    positions = (
        parse_broker_positions(
            observed["positions_body"],
            observed_at=observed_at,
            reconciliation_status="unknown",
        )
        if observed["positions_body"] is not None
        else {}
    )

    account_row = None
    reconciliation = None
    from app.services.strategy_ownership import StrategyOwnershipLedger

    ownership = StrategyOwnershipLedger.unavailable(signal.strategy, source="not_read")
    if not args.no_database:
        try:
            account_row = _broker_account()
        except Exception:  # noqa: BLE001
            account_row = None
        if account_row is None:
            blockers.append("NO_REGISTERED_PAPER_ACCOUNT")
        else:
            try:
                reconciliation = _latest_reconciliation(int(account_row["id"]))
            except Exception:  # noqa: BLE001
                reconciliation = None
            ownership = _ownership_ledger(signal.strategy, int(account_row["id"]))
    else:
        blockers.append("DATABASE_NOT_CONSULTED")

    # --- capital ------------------------------------------------------------
    buying_power = None
    raw_buying_power = observed["account"].get("buying_power")
    if raw_buying_power is not None:
        buying_power = Decimal(str(raw_buying_power))
    allocated = Decimal(str(args.allocated_capital))
    if buying_power is not None and allocated > buying_power:
        # Refused rather than silently reduced: sizing 300 names against capital
        # that is not there produces a plan nobody asked for.
        blockers.append("ALLOCATED_CAPITAL_EXCEEDS_BUYING_POWER")

    # --- the plan that would be submitted -----------------------------------
    prices = _reference_prices(symbols) if not args.no_database else {}
    rebalance = build_rebalance_plan(
        signal=signal,
        allocated_capital=allocated,
        reference_prices=prices,
        asset_facts=facts,
        positions=positions,
        ownership=ownership,
        reconciliation=reconciliation,
        buying_power=buying_power,
        share_policy=MOM_12_1_SHARE_POLICY,
    )
    verification = verify_plan_against_signal(rebalance)

    payload = {
        **_governance(),
        "signal": signal.as_dict(),
        "endpoints_used": ["/v2/account", "/v2/positions", "/v2/assets/{symbol}"],
        "mutating_endpoints_used": [],
        "account": {
            "registered": account_row is not None,
            "keftrade_account": account_row,
            "alpaca_account_id": observed["account"].get("id"),
            "alpaca_account_status": observed["account"].get("status"),
            "trading_blocked": observed["account"].get("trading_blocked"),
            "buying_power": str(buying_power) if buying_power is not None else None,
            "cash": observed["account"].get("cash"),
            "allocated_capital": str(allocated),
        },
        "reconciliation": reconciliation.as_dict() if reconciliation else None,
        "ownership": ownership.as_dict(),
        "positions": {
            "source": "alpaca_fresh_read",
            "observed_at": observed_at,
            "count": len(positions),
            # Stated at every layer, because the whole failure mode is someone
            # reading this list as the strategy's book.
            "account_positions_are_not_ownership": True,
        },
        "asset_preflight": asset_gate,
        "tradability_authority": "alpaca",
        "database_is_active_is_not_authoritative": True,
        "tradability_disagreements": disagreements,
        "preflight_blockers": blockers,
        "would_submit": rebalance.as_dict(),
        "verification": verification,
        "observed": observed["assets"],
    }
    _write(Path(args.output_dir) / f"preflight_{signal.signal_date}.json", payload)
    summary = {k: v for k, v in payload.items() if k not in ("observed", "would_submit")}
    summary["would_submit"] = {
        k: v
        for k, v in payload["would_submit"].items()
        if k not in ("symbol_plans", "exits")
    }
    return summary


def _confirmed_fills(strategy: str, broker_account_id: int) -> tuple[list, dict]:
    """Confirmed fills for this account, with the attribution of each order.

    Reads ``broker_fills`` -- executions the broker confirmed -- joined to the
    attribution table by client order id. Deliberately never reads
    ``broker_orders.filled_quantity``: an aggregate can be recomputed and
    re-applied, whereas each activity id applies exactly once.
    """
    from app.cli.stage40_audit import _open_connection
    from app.db import connect
    from app.services.strategy_ownership_lifecycle import (
        attributions_from_rows,
        fills_from_rows,
    )

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT f.broker_activity_id, f.broker_account_id, f.broker_order_id,
                   o.client_order_id, f.symbol, f.side, f.quantity, f.price,
                   f.transaction_at
              FROM broker_fills f
              LEFT JOIN broker_orders o
                     ON o.broker_account_id = f.broker_account_id
                    AND o.broker_order_id = f.broker_order_id
             WHERE f.broker_account_id = %s
             ORDER BY f.transaction_at, f.broker_activity_id
            """,
            [broker_account_id],
        )
        fill_rows = [dict(row) for row in (cursor.fetchall() or [])]
        cursor.execute(
            """
            SELECT client_order_id, strategy, strategy_version,
                   broker_account_id, symbol
              FROM strategy_order_attributions
             WHERE broker_account_id = %s AND strategy = %s
            """,
            [broker_account_id, strategy],
        )
        attribution_rows = [dict(row) for row in (cursor.fetchall() or [])]

    return fills_from_rows(fill_rows), attributions_from_rows(attribution_rows)


def ownership(args: argparse.Namespace) -> dict[str, Any]:
    """Rebuild the attributed book from confirmed fills. Read-only.

    Replays every confirmed fill for the account and reports the ledger it
    produces, alongside whatever ``strategy_owned_positions`` currently says.
    A disagreement between the two is the thing worth seeing: the stored
    aggregate is a cache of this replay, and a cache that has drifted from its
    source is not evidence of ownership.

    Writes nothing. The replay is the specification; persisting it is a
    separate, governed step that migration 081 has to land first.
    """
    from app.services.strategy_ownership_lifecycle import ownership_rows, replay_fills

    account_row = _broker_account()
    if account_row is None:
        return {**_governance(), "error": "NO_REGISTERED_PAPER_ACCOUNT"}

    account_id = int(account_row["id"])
    fills, attributions = _confirmed_fills(args.strategy, account_id)
    state = replay_fills(
        fills,
        attributions=attributions,
        strategy=args.strategy,
        strategy_version=args.strategy_version,
        broker_account_id=account_id,
    )

    stored = _ownership_ledger(args.strategy, account_id)
    replayed = state.to_ledger(source="replay")
    drift = []
    symbols = set(stored.positions) | set(state.positions)
    for symbol in sorted(symbols):
        stored_qty = stored.owned_quantity(symbol)
        replay_qty = state.owned_quantity(symbol)
        if stored_qty != replay_qty:
            drift.append(
                {
                    "symbol": symbol,
                    "stored_quantity": str(stored_qty),
                    "replayed_quantity": str(replay_qty),
                }
            )

    payload = {
        **_governance(),
        "strategy": args.strategy,
        "broker_account_id": account_id,
        "confirmed_fills_read": len(fills),
        "attributed_orders": len(attributions),
        "replayed_ledger": replayed.as_dict(),
        "stored_ledger": stored.as_dict(),
        "drift": drift,
        "faults": list(state.faults),
        "rows_that_would_be_written": [
            {**row, "quantity": str(row["quantity"]),
             "average_entry_price": (
                 str(row["average_entry_price"])
                 if row["average_entry_price"] is not None else None
             ),
             "as_of": row["as_of"]}
            for row in ownership_rows(state)
        ],
        "rows_written": 0,
        "intent_is_never_ownership_evidence": True,
    }
    _write(Path(args.output_dir) / f"ownership_{args.strategy}.json", payload)
    return {k: v for k, v in payload.items() if k != "rows_that_would_be_written"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mom12-1-portfolio",
        description=(
            "MOM_12_1 portfolio execution bridge. Observe-only: builds and "
            "verifies rebalance plans, and never submits an order."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_signal(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--signal-csv", required=True)
        cmd.add_argument(
            "--provenance",
            required=True,
            choices=[PROVENANCE_FORWARD, PROVENANCE_TEST_REPLAY],
            help=(
                "a historical CSV replayed for plumbing is indistinguishable on "
                "disk from a forward signal, so state which this is"
            ),
        )

    plan_cmd = subparsers.add_parser("plan", help="Build one observe-only plan.")
    add_signal(plan_cmd)
    plan_cmd.add_argument("--allocated-capital", required=True, type=float)
    plan_cmd.add_argument(
        "--offline",
        action="store_true",
        help="plumbing validation without a database; assumes every name is tradable",
    )
    plan_cmd.add_argument("--assumed-price", default=100.0, type=float)
    plan_cmd.set_defaults(handler=plan)

    pre_cmd = subparsers.add_parser(
        "preflight",
        help=(
            "Read-only account-aware rebalance preflight: resolves account, "
            "fresh positions, reconciliation, ownership and buying power, and "
            "prints the orders that would be submitted."
        ),
    )
    add_signal(pre_cmd)
    pre_cmd.add_argument("--allocated-capital", required=True, type=float)
    pre_cmd.add_argument(
        "--no-database",
        action="store_true",
        help=(
            "skip every KefTrade table. The result can never be a clean "
            "preflight, because ownership and reconciliation live there"
        ),
    )
    pre_cmd.set_defaults(handler=preflight)

    own_cmd = subparsers.add_parser(
        "ownership",
        help=(
            "Read-only: rebuild the attributed book from confirmed fills and "
            "report any drift from the stored ledger. Writes nothing."
        ),
    )
    own_cmd.add_argument("--strategy", default=STRATEGY_MOM_12_1)
    own_cmd.add_argument("--strategy-version", default="unknown")
    own_cmd.set_defaults(handler=ownership)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_command(
        args.handler,
        args,
        banner=f"mom12-1-portfolio-bridge :: {args.command} :: observe-only",
    )


if __name__ == "__main__":
    main()
