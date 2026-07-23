from __future__ import annotations

import argparse
import json

from app.db import connect
from app.services.research_campaigns import ensure_campaign_tables, reevaluate_elite_candidates


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        ensure_campaign_tables(conn)
        if args.command == "reevaluate":
            return reevaluate_elite_candidates(conn, campaign_id=args.campaign_id)
        raise ValueError("unsupported elites command")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade elite promotion controls (simulation only).")
    commands = root.add_subparsers(dest="command", required=True)
    reeval = commands.add_parser(
        "reevaluate",
        help="Rebuild elite status from immutable evidence under the honest consistency gate; demote candidates whose typical variant is unprofitable.",
    )
    reeval.add_argument("--campaign-id", type=int, default=None, help="Limit to one campaign (default: all campaigns).")
    return root


def main() -> None:
    print("Elite promotion | Simulation only | Median-consistency gate | No thresholds weakened")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
