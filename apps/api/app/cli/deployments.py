from __future__ import annotations

import argparse
import getpass
import json
import socket
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.db import connect
from app.services.external_execution import disable_external_deployment, enable_observe_only, enable_paper_execution, manual_halt, resume_observe_only
from app.settings import settings


def operator() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        trace_id = uuid4()
        event = f"cli_deployment_{args.command}"
        execution_reachable = settings.broker_order_submission_enabled and settings.external_paper_execution_enabled
        details = {"deployment_id": args.deployment_id, "environment": "paper", "highest_reachable_state": "enabled_execution" if execution_reachable else "enabled_observe_only"}
        conn.execute("INSERT INTO broker_audit_events(trace_id,event_type,operator,phase,details) VALUES (%s,%s,%s,'before',%s)", (trace_id, event, operator(), Jsonb(details)))
        conn.commit()
        try:
            if args.command in {"enable-external-paper", "reapprove-external-paper"}:
                if args.confirm_deployment_id != args.deployment_id:
                    raise ValueError("--confirm-deployment-id must exactly match deployment_id")
                result = enable_observe_only(conn, args.deployment_id, operator=operator(), reapprove=args.command == "reapprove-external-paper")
            elif args.command == "enable-paper-execution":
                if args.confirm_deployment_id != args.deployment_id:
                    raise ValueError("--confirm-deployment-id must exactly match deployment_id")
                result = enable_paper_execution(conn, args.deployment_id, operator=operator())
            elif args.command == "resume-external-paper":
                if args.confirm_deployment_id != args.deployment_id:
                    raise ValueError("--confirm-deployment-id must exactly match deployment_id")
                result = resume_observe_only(conn, args.deployment_id, operator=operator())
            elif args.command == "disable-external-paper":
                result = disable_external_deployment(conn, args.deployment_id, operator=operator())
            elif args.command == "halt-external-paper":
                result = manual_halt(conn, args.deployment_id, operator=operator(), reason=args.reason)
            else:
                raise ValueError("unsupported deployment command")
        except Exception as error:
            conn.rollback()
            conn.execute("INSERT INTO broker_audit_events(trace_id,event_type,operator,phase,details) VALUES (%s,%s,%s,'after',%s)", (trace_id, event, operator(), Jsonb({**details, "status": "failed", "error_class": error.__class__.__name__, "error": str(error)})))
            conn.commit()
            raise
        conn.execute("INSERT INTO broker_audit_events(trace_id,event_type,operator,phase,details) VALUES (%s,%s,%s,'after',%s)", (trace_id, event, operator(), Jsonb({**details, "status": "complete"})))
        conn.commit()
        return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade external paper deployment controls. Environment: Paper.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("enable-external-paper", "reapprove-external-paper", "resume-external-paper"):
        command = commands.add_parser(name)
        command.add_argument("deployment_id", type=int)
        command.add_argument("--confirm-deployment-id", type=int, required=True)
    disable = commands.add_parser("disable-external-paper")
    disable.add_argument("deployment_id", type=int)
    halt = commands.add_parser("halt-external-paper")
    halt.add_argument("deployment_id", type=int)
    halt.add_argument("--reason", required=True)
    execute_paper = commands.add_parser("enable-paper-execution")
    execute_paper.add_argument("deployment_id", type=int)
    execute_paper.add_argument("--confirm-deployment-id", type=int, required=True)
    return root


def main() -> None:
    print("Provider: alpaca | Environment: Paper | Live money: prohibited")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
