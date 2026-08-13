"""Backend-only CLI for event-conditioned alpha research."""

from __future__ import annotations

import argparse
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_event_discovery import (
    BRANCHES,
    declare_event_study,
    event_study_report,
    feature_catalog,
    run_event_confirmation,
    run_event_discovery,
)


def _csv(value: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return list(dict.fromkeys(values))


def _symbols(value: str) -> list[str]:
    return [item.upper() for item in _csv(value)]


def declare(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return declare_event_study(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            branches=args.branches,
            symbols=args.symbols,
            cost_calibration_id=args.cost_calibration_id,
            feed=args.feed,
            purpose=args.purpose,
        )


def discover(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return run_event_discovery(
            conn,
            declaration_id=args.declaration_id,
            max_events=args.max_events,
        )


def confirm(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return run_event_confirmation(conn, run_id=args.run_id)


def report(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return event_study_report(conn, run_id=args.run_id)


def catalog(_: argparse.Namespace) -> dict[str, Any]:
    return feature_catalog()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Pre-strategy event-conditioned alpha discovery. Measures fixed-horizon "
            "returns, MFE/MAE, normalized context, scores, and vetoes without any "
            "campaign or broker action."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    catalog_command = commands.add_parser("catalog", help="Show predeclared branches and feature jobs.")
    catalog_command.set_defaults(handler=catalog)

    declare_command = commands.add_parser("declare", help="Freeze an event-study specification before outcomes are read.")
    declare_command.add_argument("--dataset-id", type=int, required=True)
    declare_command.add_argument("--timeframe", choices=("15m", "30m"), required=True)
    declare_command.add_argument("--branches", type=_csv, required=True, help=f"Comma separated: {','.join(BRANCHES)}")
    declare_command.add_argument("--symbols", type=_symbols)
    declare_command.add_argument("--cost-calibration-id", type=int, required=True)
    declare_command.add_argument("--feed", choices=("sip", "iex"), default="sip")
    declare_command.add_argument("--purpose", required=True)
    declare_command.set_defaults(handler=declare)

    discover_command = commands.add_parser(
        "discover",
        help="Read only discovery+validation, freeze score/veto model, and leave confirmation untouched.",
    )
    discover_command.add_argument("--declaration-id", type=int, required=True)
    discover_command.add_argument(
        "--max-events",
        type=int,
        help="Operational smoke-test cap only. Omit for the governed full study.",
    )
    discover_command.set_defaults(handler=discover)

    report_command = commands.add_parser("report", help="Print a concise persisted event-study report.")
    report_command.add_argument("--run-id", type=int, required=True)
    report_command.set_defaults(handler=report)

    confirm_command = commands.add_parser(
        "confirm",
        help="Read the untouched final 20 percent exactly once using the frozen model.",
    )
    confirm_command.add_argument("--run-id", type=int, required=True)
    confirm_command.set_defaults(handler=confirm)
    return root


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "branches", None):
        unknown = sorted(set(args.branches) - set(BRANCHES))
        if unknown:
            parser().error(f"unknown branches: {unknown}")
    run_command(
        args.handler,
        args,
        banner="Intraday event-conditioned alpha discovery | pre-strategy | backend only",
    )


if __name__ == "__main__":
    main()
