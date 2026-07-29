"""VPS entry point for the backend-only 30m executable-research funnel."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.db import connect
from app.services.intraday_executable_research import (
    executable_research_status,
    freeze_factor_survivors,
    run_development_simulations,
    run_locked_confirmation_and_elite_competition,
)


def _progress(payload: dict[str, Any]) -> None:
    stage = payload.pop("stage", "research")
    detail = " · ".join(f"{key}={value}" for key, value in payload.items())
    print(f"[{stage}] {detail}", flush=True)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        if args.command == "freeze":
            return {
                "candidates": freeze_factor_survivors(
                    conn,
                    source_factor_run_id=args.source_factor_run_id,
                )
            }
        if args.command == "simulate":
            return run_development_simulations(
                conn,
                source_factor_run_id=args.source_factor_run_id,
                progress_callback=_progress,
            )
        if args.command == "confirm":
            return run_locked_confirmation_and_elite_competition(
                conn,
                source_factor_run_id=args.source_factor_run_id,
                confirmation_dataset_id=args.dataset_id,
                run_elite_campaigns=not args.no_elite_campaigns,
                campaign_batch_size=args.batch_size,
                progress_callback=_progress,
            )
        return executable_research_status(
            conn,
            source_factor_run_id=args.source_factor_run_id,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Freeze 30m factor survivors, simulate with SIP-calibrated costs, "
            "confirm on later sessions, and submit exact survivors to elite gates."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    for name in ("freeze", "simulate"):
        command = commands.add_parser(name)
        command.add_argument("--source-factor-run-id", type=int, required=True)

    confirm = commands.add_parser("confirm")
    confirm.add_argument("--source-factor-run-id", type=int, required=True)
    confirm.add_argument("--dataset-id", type=int, required=True)
    confirm.add_argument("--batch-size", type=int, default=25)
    confirm.add_argument(
        "--no-elite-campaigns",
        action="store_true",
        help="Persist locked evidence but do not run the existing elite campaign gates.",
    )

    status = commands.add_parser("status")
    status.add_argument("--source-factor-run-id", type=int)
    return root


def main() -> None:
    print("Executable intraday research | backend only | 30m", flush=True)
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()

