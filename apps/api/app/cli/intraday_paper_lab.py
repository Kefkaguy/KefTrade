from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_paper_lab import (
    AlpacaPaperLabClient,
    create_experiment,
    flatten_due_positions,
    load_lab_experiment,
    monitor,
    run_cycle,
    run_loop,
)


def _symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(symbols))


def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def create(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return create_experiment(
            conn,
            name=args.name,
            trading_date=args.trading_date,
            symbols=_symbols(args.symbols),
            calibration_id=args.calibration_id,
            max_orders_per_day=args.max_orders_per_day,
            max_open_positions=args.max_open_positions,
            quantity=args.quantity,
            allow_shorts=not args.long_only,
            feed=args.feed,
        )


def cycle(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return asyncio.run(
            run_cycle(
                conn,
                experiment_id=args.experiment_id,
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                bar_start=args.bar_start,
                feed=args.feed,
            )
        )


def loop(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return asyncio.run(
            run_loop(
                conn,
                experiment_id=args.experiment_id,
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                poll_seconds=args.poll_seconds,
                feed=args.feed,
            )
        )


def flatten(args: argparse.Namespace) -> dict:
    with connect() as conn:
        experiment = load_lab_experiment(conn, args.experiment_id)
        return {
            "experiment_id": args.experiment_id,
            "flattened": asyncio.run(
                flatten_due_positions(
                    conn,
                    experiment=experiment,
                    client=AlpacaPaperLabClient() if args.submit else None,
                    submit=args.submit,
                    now=datetime.now(tz=UTC),
                    force=True,
                )
            ),
        }


def status(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return monitor(conn, experiment_id=args.experiment_id)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Backend-only Alpaca Paper lab for unqualified signed-imbalance "
            "curiosity experiments. Live money is refused."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    create_command = commands.add_parser("create")
    create_command.add_argument("--name", required=True)
    create_command.add_argument("--trading-date", type=_date, required=True)
    create_command.add_argument("--symbols", required=True)
    create_command.add_argument("--calibration-id", type=int, required=True)
    create_command.add_argument("--max-orders-per-day", type=int, default=200)
    create_command.add_argument("--max-open-positions", type=int, default=25)
    create_command.add_argument("--quantity", type=int, default=1)
    create_command.add_argument("--feed", choices=("iex", "sip"), default="iex")
    create_command.add_argument("--long-only", action="store_true")

    cycle_command = commands.add_parser("run-cycle")
    cycle_command.add_argument("--experiment-id", type=int, required=True)
    cycle_command.add_argument("--bar-start", type=_timestamp)
    cycle_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    cycle_command.add_argument("--submit", action="store_true")
    cycle_command.add_argument("--confirm-paper", action="store_true")

    loop_command = commands.add_parser("run-loop")
    loop_command.add_argument("--experiment-id", type=int, required=True)
    loop_command.add_argument("--poll-seconds", type=int, default=300)
    loop_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    loop_command.add_argument("--submit", action="store_true")
    loop_command.add_argument("--confirm-paper", action="store_true")

    flatten_command = commands.add_parser("flatten")
    flatten_command.add_argument("--experiment-id", type=int, required=True)
    flatten_command.add_argument("--submit", action="store_true")
    flatten_command.add_argument("--confirm-paper", action="store_true")

    monitor_command = commands.add_parser("monitor")
    monitor_command.add_argument("--experiment-id", type=int, required=True)

    return root


COMMANDS = {
    "create": create,
    "run-cycle": cycle,
    "run-loop": loop,
    "flatten": flatten,
    "monitor": status,
}


def main() -> None:
    args = parser().parse_args()
    if args.command in {"flatten"} and args.submit and not args.confirm_paper:
        raise ValueError("Submitting requires --confirm-paper.")
    run_command(
        COMMANDS[args.command],
        args,
        banner="Intraday paper lab | Alpaca Paper only | backend only",
    )


if __name__ == "__main__":
    main()
