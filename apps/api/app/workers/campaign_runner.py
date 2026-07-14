from __future__ import annotations

import argparse

from app.db import connect
from app.services.research_campaigns import run_background_campaign_worker


def connection_factory():
    return connect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the KefTrade simulation-only research campaign worker.")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--stop-file", default=None)
    args = parser.parse_args()
    result = run_background_campaign_worker(
        connection_factory,
        worker_id=args.worker_id,
        poll_seconds=args.poll_seconds,
        max_cycles=args.max_cycles,
        stop_file=args.stop_file,
    )
    print(result)


if __name__ == "__main__":
    main()
