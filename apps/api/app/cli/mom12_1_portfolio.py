"""MOM_12_1 portfolio bridge CLI -- observe-only throughout.

``plan`` turns a frozen signal CSV into a complete rebalance plan and proves it
reproduces the signal. ``preflight`` asks Alpaca Paper which of the selected
names are tradable and fractionable, using read-only endpoints only.

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

    rebalance = build_rebalance_plan(
        signal=signal,
        allocated_capital=Decimal(str(args.allocated_capital)),
        reference_prices=prices,
        asset_facts=facts,
        share_policy=MOM_12_1_SHARE_POLICY,
        reconciliation_status=args.reconciliation_status,
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


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    """Ask Alpaca Paper which selected names are tradable and fractionable.

    Read-only: this uses the assets endpoint and never touches ``/v2/orders``.
    """
    import asyncio

    from app.brokers.alpaca_paper import AlpacaPaperBrokerAdapter

    signal = load_portfolio_signal(Path(args.signal_csv), provenance=args.provenance)
    symbols = list(signal.symbols)

    async def fetch() -> dict[str, Any]:
        adapter = AlpacaPaperBrokerAdapter()
        observed: dict[str, Any] = {}
        for symbol in symbols:
            try:
                response = await adapter._get(f"/v2/assets/{symbol}", "asset")
                body = response.payload or {}
                observed[symbol] = {
                    "tradable": bool(body.get("tradable")),
                    "fractionable": (
                        bool(body["fractionable"]) if "fractionable" in body else None
                    ),
                    "status": body.get("status"),
                }
            except Exception as error:  # noqa: BLE001 -- see below
                # Deliberately broad: one symbol Alpaca will not describe must
                # not abort the preflight for the other 607. The failure is
                # recorded per symbol and fails closed at the gate, because a
                # symbol we could not ask about is not a symbol we may trade
                # fractionally.
                observed[symbol] = {
                    "tradable": None,
                    "fractionable": None,
                    "error": error.__class__.__name__,
                }
        return observed

    observed = asyncio.run(fetch())
    facts = {
        symbol: AssetFact(
            symbol=symbol,
            tradable=bool(fact.get("tradable")),
            fractionable=fact.get("fractionable"),
        )
        for symbol, fact in observed.items()
    }
    from app.services.fractional_execution import preflight_assets

    result = preflight_assets(symbols, facts, share_policy=MOM_12_1_SHARE_POLICY)
    payload = {
        **_governance(),
        "signal": signal.as_dict(),
        "endpoints_used": ["/v2/assets/{symbol}"],
        "mutating_endpoints_used": [],
        "preflight": result,
        "observed": observed,
    }
    _write(Path(args.output_dir) / f"preflight_{signal.signal_date}.json", payload)
    return {k: v for k, v in payload.items() if k != "observed"}


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
    plan_cmd.add_argument("--reconciliation-status", default="clean")
    plan_cmd.add_argument(
        "--offline",
        action="store_true",
        help="plumbing validation without a database; assumes every name is tradable",
    )
    plan_cmd.add_argument("--assumed-price", default=100.0, type=float)
    plan_cmd.set_defaults(handler=plan)

    pre_cmd = subparsers.add_parser(
        "preflight", help="Read-only Alpaca Paper tradable/fractionable check."
    )
    add_signal(pre_cmd)
    pre_cmd.set_defaults(handler=preflight)
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
