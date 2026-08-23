from __future__ import annotations

import argparse
import getpass
import json
import socket

from app.db import connect
from app.services.established_paper_execution import (
    ensure_registry,
    set_execution_enabled,
)


def operator() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Explicit controls for the three established Alpaca Paper strategies."
    )
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.set_defaults(command="status")
    enable = commands.add_parser("enable")
    enable.add_argument(
        "--confirm",
        required=True,
        help="Must exactly equal: ENABLE ALPACA PAPER THREE STRATEGIES",
    )
    disable = commands.add_parser("disable")
    disable.add_argument(
        "--confirm",
        required=True,
        help="Must exactly equal: DISABLE ALPACA PAPER THREE STRATEGIES",
    )
    return root


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        ensure_registry(conn)
        if args.command == "status":
            rows = conn.execute(
                "SELECT strategy,strategy_version,symbol,enabled,enabled_at,enabled_by,latest_status,latest_error,last_evaluated_session,state FROM established_paper_strategies ORDER BY strategy"
            ).fetchall()
            return {"paper_only": True, "strategies": [dict(row) for row in rows]}
        return set_execution_enabled(
            conn,
            enabled=args.command == "enable",
            operator=operator(),
            confirmation=args.confirm,
        )


def main() -> None:
    print("Provider: Alpaca | Environment: Paper | Live money: prohibited")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
