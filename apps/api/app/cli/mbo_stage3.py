"""Stage 3 -- CLI: economic viability of the frozen Stage-2 survivors.

    python -m app.cli.mbo_stage3 plan
    python -m app.cli.mbo_stage3 freeze --stage2-results <path>
    python -m app.cli.mbo_stage3 run --stage2-results <path> --grams-dir <dir> \
        --features-dir <dir> --raw-dir <dir>

``plan`` and ``freeze`` are safe to run at any time and compute no economics.
``run`` is the expensive pass and is **not authorized** until the design has
been reviewed.

Nothing here places an order of any kind. There is no broker client imported in
this module and there is no code path that could reach one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_stage3_executor import (
    STAGE3_EXECUTOR_VERSION,
    assert_frozen_plan,
    load_frozen_survivors,
)
from app.services.mbo_stage3_plan import (
    PLAN_DESIGN_HASH,
    STAGE3_PLAN_VERSION,
    SURVIVOR_COUNT,
    SURVIVOR_HASH,
    statistical_plan,
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_stage3"


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def plan(args: argparse.Namespace) -> dict[str, Any]:
    assert_frozen_plan()
    payload = statistical_plan()
    _write(Path(args.output_dir) / "stage3_plan.json", payload)
    return payload


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    """Freeze the survivors and stop. Computes no economics."""
    assert_frozen_plan()
    frozen = load_frozen_survivors(
        Path(args.stage2_results),
        expected_count=args.expect_survivors or SURVIVOR_COUNT,
    )
    frozen["stage3_plan_design_hash"] = PLAN_DESIGN_HASH
    frozen["stage3_survivor_hash"] = SURVIVOR_HASH
    frozen["governance"] = {
        "stage2_survivors_known": True,
        "stage3_economic_outcome_viewed": False,
        "stage3_rules_frozen_before_economic_outcomes": True,
    }
    frozen["contains_economic_result"] = False
    _write(Path(args.output_dir) / "stage3_frozen_survivors.json", frozen)
    return frozen


def _load_grams(grams_dir: Path):
    """The Stage-2 sufficient statistics, keyed by cell and session date."""
    import numpy as np

    from app.services.mbo_stage2_executor import Gram

    archive = np.load(grams_dir / "stage2_grams.npz")
    cells: dict[str, dict[str, Gram]] = {}
    for key in archive.files:
        if not key.endswith("||xtx"):
            continue
        cell, session_date, _ = key.split("||")
        base = f"{cell}||{session_date}"
        yty, n, ysum = archive[f"{base}||scalars"]
        cells.setdefault(cell, {})[session_date] = Gram(
            archive[f"{base}||xtx"], archive[f"{base}||xty"], float(yty), int(n), float(ysum)
        )
    return cells


def _stage2_cell_record(results: dict[str, Any], cell: str) -> dict[str, Any]:
    cadence, horizon = cell.split("|")
    for record in results.get("cells", []):
        if record["cadence"] == cadence and record["horizon"] == horizon:
            return record
    raise ValueError(f"no Stage-2 record for survivor {cell!r}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The economic pass.

    Gated: Stage 3 was specified to stop after the design and implementation were
    produced for review. --i-have-reviewed-the-design is the reviewer's
    acknowledgement, not a default.
    """
    import numpy as np
    import pyarrow.parquet as pq

    from app.cli.mbo_stage2 import FEATURE_NAMES, _symbol_day_matrix
    from app.services.mbo_book_validator import MboBook, iter_dbn_events
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_label_engine import LABEL_OK
    from app.services.mbo_stage2_executor import split_dates
    from app.services.mbo_stage3_executor import (
        BookReplay,
        assemble_report,
        cell_prefix,
        evaluate_symbol_day,
        make_sinks,
        predict,
        query_instants,
        reconstruct_confirmation_fit,
        summarize,
        write_report,
    )
    from app.services.mbo_stage3_plan import assert_session_dates_covered

    assert_frozen_plan()
    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-3 economic pass is not authorized yet. The design and "
            "implementation were to be reviewed first; re-run with "
            "--i-have-reviewed-the-design once that review has happened."
        )

    stage2 = json.loads(Path(args.stage2_results).read_text(encoding="utf-8"))
    frozen = load_frozen_survivors(
        Path(args.stage2_results), expected_count=args.expect_survivors or SURVIVOR_COUNT
    )
    grams = _load_grams(Path(args.grams_dir))
    features_dir = Path(args.features_dir)
    labels_dir = Path(args.labels_dir)
    raw_dir = Path(args.raw_dir)

    # Reconstruct each survivor's frozen fit once, and prove it reproduces.
    session_dates = sorted({d for by_date in grams.values() for d in by_date})
    assert_session_dates_covered(session_dates)
    blocks = split_dates(session_dates)
    training = list(blocks["discovery"]) + list(blocks["validation"])
    confirmation = list(blocks["confirmation"])

    fits: dict[str, Any] = {}
    for cell in frozen["survivors"]:
        record = _stage2_cell_record(stage2, cell)
        fits[cell] = reconstruct_confirmation_fit(
            grams[cell],
            training,
            confirmation,
            record["chosen_alpha"],
            recorded_confirmation_delta_r2=(record.get("confirmation") or {}).get("delta_r2"),
            recorded_per_date_delta_r2=(record.get("confirmation") or {}).get(
                "per_date_delta_r2"
            ),
        )

    sinks = make_sinks(float(FIXED_PRICE_SCALE))
    symbol_days: list[dict[str, Any]] = []

    stems = sorted({p.name.split(".")[0] for p in features_dir.rglob("*.parquet")})
    if args.limit:
        stems = stems[: args.limit]

    for index, stem in enumerate(stems, start=1):
        symbol, _, session_date = stem.rpartition("_")
        raw_path = raw_dir / f"{stem}.mbo.dbn.zst"
        if not raw_path.is_file():
            candidates = list(raw_dir.glob(f"{stem}*.dbn.zst"))
            if not candidates:
                raise ValueError(f"no certified MBO file for {stem}")
            raw_path = candidates[0]
        if not args.quiet:
            print(f"[{index}/{len(stems)}] {stem}", flush=True)

        label_table = pq.read_table(labels_dir / f"{stem}.labels.parquet")
        label_cadence = np.asarray(
            label_table.column("cadence").to_numpy(zero_copy_only=False)
        )

        per_cell = []
        instants: set[int] = set()
        for cell in frozen["survivors"]:
            cadence, horizon = cell.split("|")
            path = features_dir / cadence / f"{stem}.{cadence}.parquet"
            if not path.is_file():
                continue
            table = pq.read_table(
                path,
                columns=["sequence_index", "feature_available_ts_recv", *FEATURE_NAMES],
            )
            design, _ = _symbol_day_matrix(table, FEATURE_NAMES)
            decision = np.asarray(
                table.column("feature_available_ts_recv").to_numpy(zero_copy_only=False),
                np.int64,
            )
            mask = label_cadence == cadence
            prefix = cell_prefix(horizon)
            status = np.asarray(
                label_table.column(f"{prefix}_status").to_numpy(zero_copy_only=False)
            )[mask]
            available = np.asarray(
                label_table.column(f"{prefix}_available_ts_recv").to_numpy(
                    zero_copy_only=False
                ),
                np.int64,
            )[mask]
            usable = (status == LABEL_OK) & np.isfinite(design).all(axis=1)
            predictions = predict(np.nan_to_num(design, nan=0.0), fits[cell]["beta"])
            per_cell.append((cell, design, predictions, decision, available, usable))
            instants.update(query_instants(decision, available, usable))

        if not per_cell:
            continue

        replay = BookReplay(MboBook)
        books = replay.run(iter_dbn_events(str(raw_path)), sorted(instants))

        for cell, _design, predictions, decision, available, usable in per_cell:
            evaluate_symbol_day(
                symbol=symbol,
                session_date=session_date,
                design=_design,
                predictions=predictions,
                decision_ts=decision,
                exit_resolution_ts=available,
                usable=usable,
                cell=cell,
                replay=replay,
                books=books,
                price_scale=float(FIXED_PRICE_SCALE),
                sinks=sinks,
            )
        symbol_days.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "candidates": int(sum(int(u.sum()) for *_, u in per_cell)),
                "bad_ts_recv_instants": len(replay.bad_recv_instants),
            }
        )
        del books, replay, per_cell

    report = assemble_report(summarize(sinks), frozen)
    report["reproduction"] = {
        cell: {
            "alpha": fit["alpha"],
            "mean_delta_r2": fit["mean_delta_r2"],
            "reproduction_verified": fit["reproduction_verified"],
        }
        for cell, fit in fits.items()
    }
    report["symbol_days"] = symbol_days
    output_dir = Path(args.output_dir)
    write_report(report, output_dir / "stage3_results.json")
    return {k: v for k, v in report.items() if k not in ("cells", "symbol_days")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-stage3",
        description="Stage 3: economic viability of the frozen Stage-2 survivors.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_cmd = subparsers.add_parser("plan", help="Emit the frozen Stage-3 plan.")
    plan_cmd.set_defaults(handler=plan)

    freeze_cmd = subparsers.add_parser(
        "freeze", help="Freeze the surviving cells from stage2_results.json."
    )
    freeze_cmd.add_argument("--stage2-results", required=True)
    freeze_cmd.add_argument("--expect-survivors", type=int, default=0)
    freeze_cmd.set_defaults(handler=freeze)

    run_cmd = subparsers.add_parser("run", help="The economic pass (gated).")
    run_cmd.add_argument("--stage2-results", required=True)
    run_cmd.add_argument("--grams-dir", required=True)
    run_cmd.add_argument("--features-dir", required=True)
    run_cmd.add_argument("--labels-dir", required=True)
    run_cmd.add_argument("--raw-dir", required=True)
    run_cmd.add_argument("--limit", type=int, default=0)
    run_cmd.add_argument("--quiet", action="store_true")
    run_cmd.add_argument("--expect-survivors", type=int, default=0)
    run_cmd.add_argument("--i-have-reviewed-the-design", action="store_true")
    run_cmd.set_defaults(handler=run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(
        args.handler,
        args,
        banner=f"{STAGE3_EXECUTOR_VERSION} :: {args.command} :: "
        f"{STAGE3_PLAN_VERSION} {PLAN_DESIGN_HASH[:12]}",
    )


if __name__ == "__main__":
    main()
