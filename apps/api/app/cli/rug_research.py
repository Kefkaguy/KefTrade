"""Read-only VPS audits and bounded, journaled intraday development experiments.

Does not create campaigns, submit orders, promote strategies, or consume the
reserved tail. Positive development results are not survivor certificates.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import psycopg
from psycopg.rows import dict_row

from app.settings import settings
from app.services.research_architecture import load_snapshot_candles, verify_dataset_snapshot
from app.services.research_campaigns import calculate_elite_paper_rollup, forward_validation_state
from app.services.rug_intraday import TIMEFRAMES, build_dataset, evaluate, generate_candidates, load_cost_model
from app.services.strategy_discovery import jsonable


def read_connection():
    return psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=5,
                           options="-c default_transaction_read_only=on -c statement_timeout=120000 -c timezone=UTC")


def audit(conn, seed):
    rows = conn.execute(
        """SELECT e.* FROM elite_research_candidates e
           JOIN research_campaigns c ON c.id = e.campaign_id
           WHERE e.simulation_only = TRUE AND c.simulation_only = TRUE
             AND c.controls->'rug'->>'seed' = %s ORDER BY e.campaign_id, e.candidate_id""",
        (str(seed),),
    ).fetchall()
    candidates = []
    for row in rows:
        candidate = {key: row.get(key) for key in
                     ("id", "candidate_id", "campaign_id", "profit_factor", "expectancy", "trade_count", "forward_validation_state")}
        candidate["historical_only"] = True
        try:
            with conn.transaction():
                rollup = calculate_elite_paper_rollup(conn, dict(row))
                candidate["recomputed_forward_evidence"] = rollup["metrics"]
                candidate["recomputed_forward_state"] = forward_validation_state(
                    rollup["metrics"], rollup["thresholds"], bool(row.get("promoted_to_paper_at") or rollup["metrics"].get("deployment_ids")))
        except Exception as error:
            candidate["audit_error"] = type(error).__name__
        candidates.append(candidate)
    return {"seed": seed, "candidates": candidates, "database_updated": False,
            "certified_strategies": 0, "certification_status": "not_established_by_this_audit"}


def prepare_pilot(conn, args):
    integrity = verify_dataset_snapshot(conn, args.dataset_id, ensure_schema=False)
    if not integrity["passed"]:
        raise ValueError("Frozen snapshot hash/count verification failed")
    datasets = {}
    issues = []
    for symbol in args.assets:
        try:
            datasets[symbol] = build_dataset(load_snapshot_candles(conn, args.dataset_id, symbol, args.timeframe), args.timeframe)
        except ValueError as error:
            issues.append({"symbol": symbol, "reason": str(error)})
    if issues:
        raise ValueError("Data preflight failed: " + json.dumps(issues))
    cost_model = load_cost_model(conn, args.cost_calibration_id, args.assets) if args.cost_calibration_id is not None else None
    return datasets, cost_model, integrity


def run_pilot(datasets, *, seed, batches, batch_size, cost_model, emit):
    evidence = []
    seen = set()
    finalists = []
    failures = Counter()
    completed_jobs = 0
    technical_errors = 0
    started = perf_counter()
    for batch_index in range(batches):
        candidates, generation = generate_candidates(max_candidates=batch_size, seed=seed, batch_index=batch_index,
                                                     evidence=evidence, cost_model=cost_model, excluded_candidate_ids=seen)
        emit({"event": "batch_started", "batch_index": batch_index, "generation": generation})
        for candidate in candidates:
            seen.add(candidate.candidate_id)
            results = []
            for symbol, dataset in datasets.items():
                try:
                    result = evaluate(candidate, dataset)
                except Exception as error:
                    technical_errors += 1
                    emit({"event": "technical_error", "candidate_id": candidate.candidate_id,
                          "symbol": symbol, "error_type": type(error).__name__, "learned_as_loss": False})
                    continue
                completed_jobs += 1
                results.append(result)
                failures.update(result["failure_reasons"])
                emit({"event": "market_result", "symbol": symbol, "result": result})
                if result["screen_passed"]:
                    finalists.append({"candidate_id": candidate.candidate_id, "symbol": symbol,
                                      "family_id": candidate.family_id, "metrics": result["metrics"],
                                      "certification": result["certification"]})
            if len(results) == len(datasets) and all(row["valid_economic_test"] for row in results):
                evidence.append({"candidate_id": candidate.candidate_id, "entry": candidate.parameters["entry"],
                                 "valid_economic_test": True, "screen_passed": any(r["screen_passed"] for r in results)})
        elapsed = perf_counter() - started
        emit({"event": "batch_complete", "batch_index": batch_index, "candidates_completed": len(evidence),
              "backtest_jobs_completed": completed_jobs, "technical_errors": technical_errors,
              "elapsed_seconds": round(elapsed, 2), "measured_jobs_per_hour": round(completed_jobs * 3600 / max(elapsed, .001), 2)})
    summary = {"event": "pilot_complete", "candidates_generated": len(seen), "valid_learning_candidates": len(evidence),
               "completed_jobs": completed_jobs, "technical_errors": technical_errors,
               "failure_counts_overlapping": dict(failures), "promising_development_markets": finalists,
               "distinct_screened_candidates": len({r["candidate_id"] for r in finalists}),
               "certified_strategies": 0, "certification_status": "final_validation_not_implemented",
               "database_updated": False, "simulation_only": True}
    emit(summary)
    return summary


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit", help="Recompute a run's elite paper evidence without changing records")
    audit_parser.add_argument("--seed", type=int, required=True)
    pilot = commands.add_parser("pilot", help="Run a bounded experiment against a verified frozen dataset")
    pilot.add_argument("--dataset-id", type=int, required=True)
    pilot.add_argument("--assets", nargs="+", default=["TSLA", "NVDA", "AAPL", "MSFT", "AMD"])
    pilot.add_argument("--timeframe", choices=TIMEFRAMES, default="15m")
    pilot.add_argument("--seed", type=int, required=True)
    pilot.add_argument("--batches", type=int, choices=range(1, 11), default=1)
    pilot.add_argument("--batch-size", type=int, choices=range(1, 101), default=25)
    pilot.add_argument("--cost-calibration-id", type=int)
    pilot.add_argument("--output", type=Path, required=True, help="New JSONL journal path; existing files are never overwritten")
    return root


def main():
    args = parser().parse_args()
    if args.command == "audit":
        with read_connection() as conn:
            print(json.dumps(jsonable(audit(conn, args.seed)), indent=2, allow_nan=False))
        return
    args.assets = sorted({asset.upper() for asset in args.assets})
    if not 1 <= len(args.assets) <= 10:
        raise SystemExit("Pilot requires between one and ten distinct assets")
    # Verify before opening a journal; release the database transaction before
    # running CPU-heavy simulations. Database credentials never enter output.
    with read_connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        datasets, cost_model, integrity = prepare_pilot(conn, args)
    with args.output.open("x", encoding="utf-8") as journal:
        def emit(event):
            journal.write(json.dumps(jsonable(event), allow_nan=False) + "\n")
            journal.flush()
            if event["event"] != "market_result":
                print(json.dumps(jsonable(event), allow_nan=False), flush=True)
        emit({"event": "pilot_started", "started_at": datetime.now(UTC), "seed": args.seed,
              "dataset_id": args.dataset_id, "timeframe": args.timeframe, "assets": args.assets,
              "integrity": integrity, "cost_calibration_id": args.cost_calibration_id,
              "batch_size": args.batch_size, "batches": args.batches,
              "maximum_backtest_jobs": args.batch_size * args.batches * len(args.assets),
              "simulations_per_job": 9, "simulation_only": True})
        run_pilot(datasets, seed=args.seed, batches=args.batches, batch_size=args.batch_size,
                  cost_model=cost_model, emit=emit)


if __name__ == "__main__":
    main()
