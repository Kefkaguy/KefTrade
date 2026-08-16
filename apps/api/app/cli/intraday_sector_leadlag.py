"""CLI for governed five-minute sector peer lead/lag research."""

from __future__ import annotations

import argparse
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_sector_leadlag import (
    confirm_sector_leadlag,
    declare_sector_leadlag,
    preflight_sector_leadlag,
    run_sector_leadlag_discovery,
    sector_leadlag_report,
)


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return preflight_sector_leadlag(conn, dataset_id=args.dataset_id)


def declare(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return declare_sector_leadlag(
            conn,
            dataset_id=args.dataset_id,
            cost_calibration_id=args.cost_calibration_id,
            prior_effective_trials=args.prior_effective_trials,
            purpose=args.purpose,
        )


def discover(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return run_sector_leadlag_discovery(conn, declaration_id=args.declaration_id)


def report(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return sector_leadlag_report(conn, run_id=args.run_id)


def confirm(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return confirm_sector_leadlag(conn, run_id=args.run_id)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Governed 5m cross-asset falsification study: completed leave-one-out "
            "sector-peer excess return versus SPY predicts the target's subsequent "
            "SPY-relative +5/+10/+15m return."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    p = commands.add_parser(
        "preflight",
        help="Measure predictor/state supply and future timestamp coverage without reading forward prices.",
    )
    p.add_argument("--dataset-id", type=int, required=True)
    p.set_defaults(handler=preflight)

    d = commands.add_parser("declare", help="Freeze the six-cell lead/lag specification.")
    d.add_argument("--dataset-id", type=int, required=True)
    d.add_argument("--cost-calibration-id", type=int, required=True)
    d.add_argument(
        "--prior-effective-trials",
        type=int,
        required=True,
        help="Cumulative effective trials already spent on this historical tape before these six cells.",
    )
    d.add_argument("--purpose", required=True)
    d.set_defaults(handler=declare)

    x = commands.add_parser(
        "discover",
        help="Spend discovery+validation once for one frozen declaration.",
    )
    x.add_argument("--declaration-id", type=int, required=True)
    x.set_defaults(handler=discover)

    r = commands.add_parser("report", help="Print persisted evidence without re-running it.")
    r.add_argument("--run-id", type=int, required=True)
    r.set_defaults(handler=report)

    c = commands.add_parser(
        "confirm",
        help="Spend untouched confirmation once, and only for promoted candidate cells.",
    )
    c.add_argument("--run-id", type=int, required=True)
    c.set_defaults(handler=confirm)

    return root


def main() -> None:
    args = parser().parse_args()
    run_command(
        args.handler,
        args,
        banner="5m sector peer lead/lag | 2 states x 3 horizons | governed evidence",
    )


if __name__ == "__main__":
    main()
