from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_paper_lab import (
    AlpacaPaperLabClient,
    EVIDENCE_BASES,
    EXCHANGE,
    GAP_PAPER_LAB_FACTORS,
    REGULAR_CLOSE,
    REGULAR_OPEN,
    _utc,
    create_experiment,
    create_gap_factor_experiment,
    freeze_experiment,
    flatten_due_positions,
    load_lab_experiment,
    monitor,
    run_cycle,
    run_gap_factor_cycle,
    run_gap_factor_loop,
    run_loop,
)


def _symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(symbols))


def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _ids(value: str | None) -> list[int]:
    if not value:
        return []
    ids: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            ids.append(int(item))
    return list(dict.fromkeys(ids))


def create(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return create_experiment(
            conn,
            name=args.name,
            trading_date=args.trading_date,
            symbols=_symbols(args.symbols),
            calibration_id=args.calibration_id,
            max_orders_per_day=args.max_orders_per_day,
            max_open_positions=args.max_open_positions,
            quantity=args.quantity,
            allow_shorts=not args.long_only,
            feed=args.feed,
            evidence_basis=args.evidence_basis,
            alpha_map_run_id=args.alpha_map_run_id,
            alpha_map_cell_key=args.alpha_map_cell_key,
        )


def create_gap(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return create_gap_factor_experiment(
            conn,
            name=args.name,
            trading_date=args.trading_date,
            symbols=_symbols(args.symbols),
            factor_key=args.factor_key,
            max_orders_per_day=args.max_orders_per_day,
            max_open_positions=args.max_open_positions,
            quantity=args.quantity,
            allow_shorts=not args.long_only,
            feed=args.feed,
            evidence_basis=args.evidence_basis,
            alpha_map_run_id=args.alpha_map_run_id,
            alpha_map_cell_key=args.alpha_map_cell_key,
        )


def freeze(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return freeze_experiment(
            conn,
            experiment_id=args.experiment_id,
            finding=args.finding,
        )


def cycle(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return asyncio.run(
            run_cycle(
                conn,
                experiment_id=args.experiment_id,
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                bar_start=args.bar_start,
                feed=args.feed,
            )
        )


def gap_cycle(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return asyncio.run(
            run_gap_factor_cycle(
                conn,
                experiment_id=args.experiment_id,
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                bar_start=args.bar_start,
                feed=args.feed,
            )
        )


def loop(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return asyncio.run(
            run_loop(
                conn,
                experiment_id=args.experiment_id,
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                poll_seconds=args.poll_seconds,
                feed=args.feed,
            )
        )


def gap_loop(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return asyncio.run(
            run_gap_factor_loop(
                conn,
                experiment_id=args.experiment_id,
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                poll_seconds=args.poll_seconds,
                feed=args.feed,
            )
        )


def flatten(args: argparse.Namespace) -> dict:
    with connect() as conn:
        experiment = load_lab_experiment(conn, args.experiment_id)
        return {
            "experiment_id": args.experiment_id,
            "flattened": asyncio.run(
                flatten_due_positions(
                    conn,
                    experiment=experiment,
                    client=AlpacaPaperLabClient() if args.submit else None,
                    submit=args.submit,
                    now=datetime.now(tz=UTC),
                    force=True,
                )
            ),
        }


def status(args: argparse.Namespace) -> dict:
    with connect() as conn:
        return monitor(conn, experiment_id=args.experiment_id)


async def _run_scheduled_experiment(
    experiment_id: int,
    *,
    submit: bool,
    confirm_paper: bool,
    poll_seconds: int,
    feed: str | None,
) -> dict:
    with connect() as conn:
        experiment = load_lab_experiment(conn, experiment_id)
        conn.execute(
            "UPDATE intraday_paper_lab_experiments SET status='running', updated_at=NOW() WHERE id=%s",
            (experiment_id,),
        )
        conn.commit()
        config = dict(experiment.get("config") or {})
        factor_key = str(config.get("factor_key") or experiment.get("factor_key") or "")
        if factor_key in GAP_PAPER_LAB_FACTORS:
            return await run_gap_factor_loop(
                conn,
                experiment_id=experiment_id,
                submit=submit,
                confirm_paper=confirm_paper,
                poll_seconds=poll_seconds,
                feed=feed,
            )
        return await run_loop(
            conn,
            experiment_id=experiment_id,
            submit=submit,
            confirm_paper=confirm_paper,
            poll_seconds=poll_seconds,
            feed=feed,
        )


async def _schedule_async(args: argparse.Namespace) -> dict:
    trading_date: date = args.trading_date or datetime.now(tz=EXCHANGE).date()
    start_at = _utc(trading_date, REGULAR_OPEN) - timedelta(minutes=args.start_minutes_before_open)
    session_close = _utc(trading_date, REGULAR_CLOSE) + timedelta(minutes=10)
    now = datetime.now(tz=UTC)
    if now > session_close:
        return {
            "status": "refused",
            "reason": "trading_date session is already closed",
            "trading_date": trading_date,
            "session_close": session_close,
        }
    if now < start_at and not args.dry_run:
        while datetime.now(tz=UTC) < start_at:
            await asyncio.sleep(min(60, max(1, int((start_at - datetime.now(tz=UTC)).total_seconds()))))

    requested_ids = _ids(args.experiment_ids)
    with connect() as conn:
        if requested_ids:
            rows = conn.execute(
                """
                SELECT id, name, status, factor_key, trading_date, config
                FROM intraday_paper_lab_experiments
                WHERE trading_date = %s
                  AND id = ANY(%s)
                ORDER BY id
                """,
                (trading_date, requested_ids),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, status, factor_key, trading_date, config
                FROM intraday_paper_lab_experiments
                WHERE trading_date = %s
                  AND status IN ('created', 'running')
                ORDER BY id
                """,
                (trading_date,),
            ).fetchall()
    experiments = [dict(row) for row in rows]
    if args.dry_run:
        return {
            "status": "dry_run",
            "trading_date": trading_date,
            "start_at": start_at,
            "session_close": session_close,
            "experiments": experiments,
        }
    if not experiments:
        return {
            "status": "no_experiments",
            "trading_date": trading_date,
            "start_at": start_at,
            "session_close": session_close,
        }
    results = await asyncio.gather(
        *[
            _run_scheduled_experiment(
                int(experiment["id"]),
                submit=args.submit,
                confirm_paper=args.confirm_paper,
                poll_seconds=args.poll_seconds,
                feed=args.feed,
            )
            for experiment in experiments
        ],
        return_exceptions=True,
    )
    normalized_results: list[dict] = []
    for experiment, result in zip(experiments, results, strict=False):
        if isinstance(result, Exception):
            normalized_results.append(
                {
                    "experiment_id": experiment["id"],
                    "status": "error",
                    "error": str(result),
                    "error_type": type(result).__name__,
                }
            )
        else:
            normalized_results.append(dict(result))
    return {
        "status": "completed",
        "trading_date": trading_date,
        "experiments_started": len(experiments),
        "results": normalized_results,
    }


def schedule(args: argparse.Namespace) -> dict:
    return asyncio.run(_schedule_async(args))


def _add_evidence_arguments(command: argparse.ArgumentParser) -> None:
    """Make every experiment state what it is evidence about.

    Paper Lab cannot establish whether a feature predicts anything -- a dozen
    fake trades in an afternoon is not a sample -- so an experiment has to
    declare whether it is reproducing an already-measured effect under real
    execution, or exercising the plumbing. Both are legitimate; conflating them
    is what turned a curiosity run into an apparent verdict on a hypothesis.
    """
    command.add_argument(
        "--evidence-basis",
        choices=EVIDENCE_BASES,
        required=True,
        help=(
            "alpha_map_cleared: an alpha-map cell measured this effect and this run "
            "tests whether it survives execution reality. "
            "operational_curiosity: this run tests scheduling, fills and broker "
            "semantics, and its P/L says nothing about the hypothesis."
        ),
    )
    command.add_argument("--alpha-map-run-id", type=int)
    command.add_argument(
        "--alpha-map-cell-key",
        help="Required with --evidence-basis alpha_map_cleared.",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Backend-only Alpaca Paper lab for unqualified signed-imbalance "
            "curiosity experiments. Live money is refused."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    create_command = commands.add_parser("create")
    create_command.add_argument("--name", required=True)
    create_command.add_argument("--trading-date", type=_date, required=True)
    create_command.add_argument("--symbols", required=True)
    create_command.add_argument("--calibration-id", type=int, required=True)
    create_command.add_argument("--max-orders-per-day", type=int, default=200)
    create_command.add_argument("--max-open-positions", type=int, default=25)
    create_command.add_argument("--quantity", type=int, default=1)
    create_command.add_argument("--feed", choices=("iex", "sip"), default="iex")
    create_command.add_argument("--long-only", action="store_true")
    _add_evidence_arguments(create_command)

    create_gap_command = commands.add_parser("create-gap")
    create_gap_command.add_argument("--name", required=True)
    create_gap_command.add_argument("--trading-date", type=_date, required=True)
    create_gap_command.add_argument("--symbols", required=True)
    create_gap_command.add_argument("--factor-key", choices=sorted(GAP_PAPER_LAB_FACTORS), required=True)
    create_gap_command.add_argument("--max-orders-per-day", type=int, default=200)
    create_gap_command.add_argument("--max-open-positions", type=int, default=25)
    create_gap_command.add_argument("--quantity", type=int, default=1)
    create_gap_command.add_argument("--feed", choices=("iex", "sip"), default="sip")
    create_gap_command.add_argument("--long-only", action="store_true")
    _add_evidence_arguments(create_gap_command)

    freeze_command = commands.add_parser(
        "freeze",
        help=(
            "Preserve an experiment as a finding. It becomes unrunnable, so its "
            "hypothesis cannot be quietly re-fitted under the same name."
        ),
    )
    freeze_command.add_argument("--experiment-id", type=int, required=True)
    freeze_command.add_argument(
        "--finding",
        required=True,
        help="What the result says about the hypothesis, not what the P/L was.",
    )

    cycle_command = commands.add_parser("run-cycle")
    cycle_command.add_argument("--experiment-id", type=int, required=True)
    cycle_command.add_argument("--bar-start", type=_timestamp)
    cycle_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    cycle_command.add_argument("--submit", action="store_true")
    cycle_command.add_argument("--confirm-paper", action="store_true")

    loop_command = commands.add_parser("run-loop")
    loop_command.add_argument("--experiment-id", type=int, required=True)
    loop_command.add_argument("--poll-seconds", type=int, default=300)
    loop_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    loop_command.add_argument("--submit", action="store_true")
    loop_command.add_argument("--confirm-paper", action="store_true")

    gap_cycle_command = commands.add_parser("run-gap-cycle")
    gap_cycle_command.add_argument("--experiment-id", type=int, required=True)
    gap_cycle_command.add_argument("--bar-start", type=_timestamp)
    gap_cycle_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    gap_cycle_command.add_argument("--submit", action="store_true")
    gap_cycle_command.add_argument("--confirm-paper", action="store_true")

    gap_loop_command = commands.add_parser("run-gap-loop")
    gap_loop_command.add_argument("--experiment-id", type=int, required=True)
    gap_loop_command.add_argument("--poll-seconds", type=int, default=300)
    gap_loop_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    gap_loop_command.add_argument("--submit", action="store_true")
    gap_loop_command.add_argument("--confirm-paper", action="store_true")

    flatten_command = commands.add_parser("flatten")
    flatten_command.add_argument("--experiment-id", type=int, required=True)
    flatten_command.add_argument("--submit", action="store_true")
    flatten_command.add_argument("--confirm-paper", action="store_true")

    monitor_command = commands.add_parser("monitor")
    monitor_command.add_argument("--experiment-id", type=int, required=True)

    schedule_command = commands.add_parser("schedule")
    schedule_command.add_argument("--trading-date", type=_date)
    schedule_command.add_argument("--experiment-ids", default=None)
    schedule_command.add_argument("--poll-seconds", type=int, default=60)
    schedule_command.add_argument("--feed", choices=("iex", "sip"), default=None)
    schedule_command.add_argument("--start-minutes-before-open", type=int, default=10)
    schedule_command.add_argument("--submit", action="store_true")
    schedule_command.add_argument("--confirm-paper", action="store_true")
    schedule_command.add_argument("--dry-run", action="store_true")

    return root


COMMANDS = {
    "create": create,
    "create-gap": create_gap,
    "freeze": freeze,
    "run-cycle": cycle,
    "run-gap-cycle": gap_cycle,
    "run-loop": loop,
    "run-gap-loop": gap_loop,
    "flatten": flatten,
    "monitor": status,
    "schedule": schedule,
}


def main() -> None:
    args = parser().parse_args()
    if args.command in {"flatten", "run-cycle", "run-loop", "run-gap-cycle", "run-gap-loop", "schedule"} and args.submit and not args.confirm_paper:
        raise ValueError("Submitting requires --confirm-paper.")
    run_command(
        COMMANDS[args.command],
        args,
        banner="Intraday paper lab | Alpaca Paper only | backend only",
    )


if __name__ == "__main__":
    main()
