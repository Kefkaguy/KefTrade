from __future__ import annotations

import argparse
import json

from app.db import connect
from app.services.elite_portfolio_activation import repair_stalled_activation_attempts
from app.services.research_campaigns import ensure_campaign_tables, reevaluate_elite_candidates


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        if args.command == "reevaluate":
            ensure_campaign_tables(conn)
            return reevaluate_elite_candidates(conn, campaign_id=args.campaign_id)
        if args.command == "repair-stalled-activations":
            return {"repaired": repair_stalled_activation_attempts(conn)}
        raise ValueError("unsupported elites command")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade elite promotion controls (simulation only).")
    commands = root.add_subparsers(dest="command", required=True)
    reeval = commands.add_parser(
        "reevaluate",
        help="Rebuild elite status from immutable evidence under the honest consistency gate; demote candidates whose typical variant is unprofitable.",
    )
    reeval.add_argument("--campaign-id", type=int, default=None, help="Limit to one campaign (default: all campaigns).")
    commands.add_parser(
        "repair-stalled-activations",
        help=(
            "Finish elite portfolio activation attempts left 'running' or 'partial' by the datetime-serialization "
            "bug fixed in elite_portfolio_activation.py. Safe to run any time: replaying an attempt whose members "
            "are already in a terminal state creates nothing new."
        ),
    )
    return root


def main() -> None:
    print("Elite promotion | Simulation only | Median-consistency gate | No thresholds weakened")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
