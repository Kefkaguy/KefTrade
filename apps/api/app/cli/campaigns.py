from __future__ import annotations

import argparse
import getpass
import json
import socket

from app.db import connect
from app.services.research_campaigns import (
    campaign_progress_breakdown,
    ensure_campaign_tables,
    repair_campaign,
)


def operator() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        ensure_campaign_tables(conn)
        if args.command == "progress":
            return campaign_progress_breakdown(conn, args.campaign_id)
        if args.command == "repair":
            if args.confirm_campaign_id != args.campaign_id:
                raise ValueError("--confirm-campaign-id must exactly match campaign_id")
            return repair_campaign(
                conn,
                args.campaign_id,
                operator=operator(),
                terminalize_exhausted_blocks=not args.keep_blocked,
            )
        raise ValueError("unsupported campaign command")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade research campaign reliability controls (simulation only).")
    commands = root.add_subparsers(dest="command", required=True)

    progress = commands.add_parser("progress", help="Show authoritative progress and invariant state for a campaign.")
    progress.add_argument("campaign_id", type=int)

    repair = commands.add_parser("repair", help="Deterministically recover a stuck campaign and finalize when all jobs are terminal.")
    repair.add_argument("campaign_id", type=int)
    repair.add_argument("--confirm-campaign-id", type=int, required=True)
    repair.add_argument(
        "--keep-blocked",
        action="store_true",
        help="Do NOT terminalize retry-exhausted blocked jobs (leave them blocked_data for a later data refresh).",
    )
    return root


def main() -> None:
    print("Campaign controls | Simulation only | No thresholds are weakened by repair")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
