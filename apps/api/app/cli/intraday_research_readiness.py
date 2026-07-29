"""VPS CLI for backend-only intraday research-data readiness."""

from __future__ import annotations

import argparse
import json

from app.db import connect
from app.services.intraday_research_data import research_data_readiness


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return research_data_readiness(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            universe_key=args.universe_key,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Audit backend data required for institutional 15m/30m research."
    )
    root.add_argument("--dataset-id", type=int, required=True)
    root.add_argument("--timeframe", choices=("15m", "30m"), required=True)
    root.add_argument("--universe-key")
    return root


def main() -> None:
    print("Intraday research data readiness | backend only", flush=True)
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
