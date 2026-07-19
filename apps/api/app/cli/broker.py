from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import socket
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.db import connect
from app.services.broker_reconciliation import reconcile_broker_snapshot, upsert_halt
from app.services.broker_sync import synchronize_broker
from app.services.external_execution import resume_observe_only, validate_adapter_compatibility
from app.settings import settings


def operator() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def audit(conn, trace_id: UUID, event_type: str, phase: str, details: dict) -> None:
    conn.execute("INSERT INTO broker_audit_events(trace_id,event_type,operator,phase,details) VALUES (%s,%s,%s,%s,%s)", (trace_id, event_type, operator(), phase, Jsonb(details)))
    conn.commit()


async def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        trace_id = uuid4()
        audit(conn, trace_id, f"cli_broker_{args.command}", "before", {"environment": "paper", "base_url": settings.alpaca_paper_base_url})
        try:
            if args.command == "sync":
                result = await synchronize_broker(conn)
            elif args.command == "reconcile":
                sync = conn.execute("SELECT * FROM broker_sync_runs WHERE status='complete' ORDER BY completed_at DESC LIMIT 1").fetchone()
                if not sync:
                    raise ValueError("no complete broker sync exists")
                result = reconcile_broker_snapshot(conn, int(sync["id"]))
            elif args.command == "halt":
                halt = upsert_halt(conn, trace_id, "global", "alpaca-paper", "manual_halt", {"reason": args.reason, "operator": operator()})
                conn.commit()
                result = {"halt": halt, "state": "halted"}
            elif args.command == "resume":
                account = conn.execute("SELECT * FROM broker_accounts ORDER BY last_successful_sync_at DESC NULLS LAST LIMIT 1").fetchone()
                if not account or args.confirm_account != account["account_number_masked"]:
                    raise ValueError("--confirm-account must exactly match the displayed masked paper account")
                deployments = conn.execute("SELECT id FROM external_paper_deployments WHERE state='manually_halted' ORDER BY id").fetchall()
                result = {"resumed": [resume_observe_only(conn, int(row["id"]), operator=operator()) for row in deployments]}
            elif args.command == "validate-adapter-compatibility":
                result = validate_adapter_compatibility(conn, operator=operator())
            else:
                raise ValueError("unsupported broker command")
        except Exception as error:
            conn.rollback()
            audit(conn, trace_id, f"cli_broker_{args.command}", "after", {"status": "failed", "error_class": error.__class__.__name__, "error": str(error), "broker_mutation": False})
            raise
        audit(conn, trace_id, f"cli_broker_{args.command}", "after", {"status": result.get("status", result.get("state", "complete")), "broker_mutation": False})
        return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade Alpaca Paper administrative controls. Provider environment: paper.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    commands.add_parser("reconcile")
    halt = commands.add_parser("halt")
    halt.add_argument("--reason", required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--confirm-account", required=True)
    commands.add_parser("validate-adapter-compatibility")
    return root


def main() -> None:
    print("Provider: alpaca | Environment: Paper | Order submission: disabled")
    result = asyncio.run(execute(parser().parse_args()))
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
