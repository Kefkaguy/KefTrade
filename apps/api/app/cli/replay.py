from __future__ import annotations

import argparse
import json

from app.db import connect
from app.services.elite_replay_outcomes import run_replay_outcomes
from app.services.elite_shadow_replay import run_elite_shadow_replay
from app.services.research_campaigns import refresh_command_center_aggregate_snapshot


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        if args.command == "elite-shadow":
            return run_elite_shadow_replay(
                conn,
                external_deployment_id=args.external_deployment_id,
                candle_limit=args.candle_limit,
            )
        if args.command == "outcomes":
            return run_replay_outcomes(conn, replay_run_id=args.replay_run_id)
        if args.command == "refresh-command-center":
            payload = refresh_command_center_aggregate_snapshot(conn)
            conn.commit()
            return {
                "status": "complete",
                "overview": payload["overview"],
                "source": payload["source"],
                "simulation_only": True,
                "broker_mutation": False,
            }
        raise ValueError("unsupported replay command")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade historical replay and research snapshot tools. No broker mutation is reachable.")
    commands = root.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("elite-shadow", help="Replay frozen active elites over historical candles.")
    replay.add_argument("--external-deployment-id", type=int)
    replay.add_argument("--candle-limit", type=int, default=2000)
    outcomes = commands.add_parser("outcomes", help="Calculate stop/target outcomes, performance, regimes, and timing compatibility.")
    outcomes.add_argument("--replay-run-id", type=int)
    commands.add_parser("refresh-command-center", help="Refresh authoritative candidate counts without deleting evidence.")
    return root


def main() -> None:
    print("Mode: historical simulation | Broker mutation: impossible")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
