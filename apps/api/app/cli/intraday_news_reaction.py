"""CLI for governed five-minute point-in-time news-reaction research."""

from __future__ import annotations

import argparse
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_news_reaction import (
    confirm_news_reaction,
    news_reaction_report,
    run_news_reaction_discovery,
)
from app.services.intraday_news_reaction_governed import (
    declare_news_reaction,
    preflight_news_reaction,
)


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return preflight_news_reaction(conn, dataset_id=args.dataset_id)


def declare(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return declare_news_reaction(
            conn,
            dataset_id=args.dataset_id,
            cost_calibration_id=args.cost_calibration_id,
            prior_effective_trials=args.prior_effective_trials,
            purpose=args.purpose,
        )


def discover(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return run_news_reaction_discovery(conn, declaration_id=args.declaration_id)


def report(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return news_reaction_report(conn, run_id=args.run_id)


def confirm(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return confirm_news_reaction(conn, run_id=args.run_id)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Pre-strategy 5m company-news reaction research. News triggers the event; "
            "the first completed five minutes define continuation/failure state; "
            "fixed +5/+10/+15/+30m outcomes are measured only after declaration."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    p = commands.add_parser("preflight", help="Count frozen event supply without reading forward outcomes.")
    p.add_argument("--dataset-id", type=int, required=True)
    p.set_defaults(handler=preflight)

    d = commands.add_parser("declare", help="Freeze the entire 16-cell research specification.")
    d.add_argument("--dataset-id", type=int, required=True)
    d.add_argument("--cost-calibration-id", type=int, required=True)
    d.add_argument(
        "--prior-effective-trials",
        type=int,
        required=True,
        help="Cumulative effective trials already spent on this historical tape before these 16 cells.",
    )
    d.add_argument("--purpose", required=True)
    d.set_defaults(handler=declare)

    x = commands.add_parser("discover", help="Spend discovery+validation once for one frozen declaration.")
    x.add_argument("--declaration-id", type=int, required=True)
    x.set_defaults(handler=discover)

    r = commands.add_parser("report", help="Print a persisted discovery report without re-running evidence.")
    r.add_argument("--run-id", type=int, required=True)
    r.set_defaults(handler=report)

    c = commands.add_parser("confirm", help="Spend untouched confirmation once, only for promoted cells.")
    c.add_argument("--run-id", type=int, required=True)
    c.set_defaults(handler=confirm)

    return root


def main() -> None:
    args = parser().parse_args()
    run_command(
        args.handler,
        args,
        banner="5m point-in-time news reaction | pre-strategy | governed evidence",
    )


if __name__ == "__main__":
    main()
