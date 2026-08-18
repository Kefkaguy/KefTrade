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


def _symbol_day_stems(features_dir: Path) -> list[str]:
    return sorted({p.name.split(".")[0] for p in features_dir.rglob("*.parquet")})


def _read_cell_inputs(
    *, features_dir: Path, labels_dir: Path, stem: str, cell: str, beta
):
    """Design, predictions, decision instants and exit resolutions for one cell.

    The design matrix is built by the Stage-2 loader itself, so the features
    Stage 3 predicts on are the same objects Stage 2 fitted on rather than a
    re-implementation that could drift apart.
    """
    import numpy as np
    import pyarrow.parquet as pq

    from app.cli.mbo_stage2 import FEATURE_NAMES, _symbol_day_matrix
    from app.services.mbo_label_engine import LABEL_OK
    from app.services.mbo_stage3_executor import (
        cell_prefix,
        certify_spine,
        event_horizon_availability,
        predict,
        session_return_bps,
    )

    cadence, horizon = cell.split("|")
    path = features_dir / cadence / f"{stem}.{cadence}.parquet"
    if not path.is_file():
        return None

    table = pq.read_table(
        path,
        columns=[
            "sequence_index",
            "ts_event",
            "feature_available_ts_recv",
            *FEATURE_NAMES,
        ],
    )
    design, sequence = _symbol_day_matrix(table, FEATURE_NAMES)
    decision = np.asarray(
        table.column("feature_available_ts_recv").to_numpy(zero_copy_only=False),
        np.int64,
    )
    midpoints = np.asarray(
        table.column("midpoint").to_numpy(zero_copy_only=False), float
    )

    prefix = cell_prefix(horizon)
    labels = pq.read_table(
        labels_dir / f"{stem}.labels.parquet",
        columns=[
            "cadence",
            "sequence_index",
            "source_ts_event",
            "source_midpoint",
            f"{prefix}_status",
            f"{prefix}_available_ts_recv",
        ],
    )
    mask = np.asarray(labels.column("cadence").to_numpy(zero_copy_only=False)) == cadence

    # The full Stage-2 certification, not just row ordering: two extractions can
    # agree on sequence_index while describing different instants and prices.
    certify_spine(
        stem,
        cadence,
        feature_sequence=sequence,
        feature_ts_event=np.asarray(
            table.column("ts_event").to_numpy(zero_copy_only=False), np.int64
        ),
        feature_midpoint=np.asarray(
            table.column("midpoint").to_numpy(zero_copy_only=False), float
        ),
        label_sequence=np.asarray(
            labels.column("sequence_index").to_numpy(zero_copy_only=False), np.int64
        )[mask],
        label_ts_event=np.asarray(
            labels.column("source_ts_event").to_numpy(zero_copy_only=False), np.int64
        )[mask],
        label_midpoint=np.asarray(
            labels.column("source_midpoint").to_numpy(zero_copy_only=False), float
        )[mask],
    )

    status = np.asarray(
        labels.column(f"{prefix}_status").to_numpy(zero_copy_only=False)
    )[mask]
    # Nullable by design: a non-OK label has no resolution instant.
    availability = event_horizon_availability(
        status, labels.column(f"{prefix}_available_ts_recv").filter(mask)
    )

    finite = np.isfinite(design).all(axis=1)
    usable = (status == LABEL_OK) & finite
    predictions = predict(np.nan_to_num(design, nan=0.0), beta)
    return {
        "design": design,
        "predictions": predictions,
        "decision": decision,
        "availability": availability,
        "usable": usable,
        "session_return_bps": session_return_bps(midpoints),
    }


def _prepare(args: argparse.Namespace, *, economic: bool) -> dict[str, Any]:
    """Everything both `run` and `diagnose` need, with every gate applied."""
    import numpy as np

    from app.services.mbo_stage2_executor import split_dates
    from app.services.mbo_stage3_executor import (
        assert_batch_complete,
        assert_feature_batch_is_frozen,
        discovery_decile_threshold,
        reconstruct_confirmation_fit,
    )
    from app.services.mbo_stage3_plan import assert_session_dates_covered

    assert_frozen_plan()
    features_dir = Path(args.features_dir)
    labels_dir = Path(args.labels_dir)

    # (3) The supplied features must be the frozen engine's own output. Correct
    # Grams say nothing about whichever directory was passed in here.
    assert_feature_batch_is_frozen(
        json.loads((features_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    )

    stage2 = json.loads(Path(args.stage2_results).read_text(encoding="utf-8"))
    frozen = load_frozen_survivors(
        Path(args.stage2_results), expected_count=SURVIVOR_COUNT
    )

    # The authorized run may not infer its universe from whatever Parquets are
    # present. A missing confirmation symbol-day is a refusal, not a smaller
    # sample. The diagnostic command deliberately examines a subset, so it is
    # exempt -- and it cannot produce an economic result.
    completeness: dict[str, Any] | None = None
    if economic:
        completeness = assert_batch_complete(
            features_dir=features_dir,
            labels_dir=labels_dir,
            grams_dir=Path(args.grams_dir),
            stage2_results=stage2,
        )

    grams = _load_grams(Path(args.grams_dir))

    session_dates = sorted({d for by_date in grams.values() for d in by_date})
    assert_session_dates_covered(session_dates)
    blocks = split_dates(session_dates)
    training = list(blocks["discovery"]) + list(blocks["validation"])
    confirmation = list(blocks["confirmation"])

    fits: dict[str, Any] = {}
    for cell in frozen["survivors"]:
        record = _stage2_cell_record(stage2, cell)
        confirm_block = record.get("confirmation") or {}
        fits[cell] = reconstruct_confirmation_fit(
            grams[cell],
            training,
            confirmation,
            record["chosen_alpha"],
            recorded_confirmation_delta_r2=confirm_block.get("delta_r2"),
            recorded_per_date_delta_r2=confirm_block.get("per_date_delta_r2"),
        )

    stems = _symbol_day_stems(features_dir)
    by_date: dict[str, list[str]] = {}
    for stem in stems:
        _symbol, _, session_date = stem.rpartition("_")
        by_date.setdefault(session_date, []).append(stem)

    # (5) Calibrate the secondary rule's threshold on DISCOVERY predictions only.
    # Predictions, not outcomes; no book is replayed and no economics accumulated.
    deciles: dict[str, float | None] = {}
    for cell in frozen["survivors"]:
        pooled: list[float] = []
        for session_date in blocks["discovery"]:
            for stem in by_date.get(session_date, []):
                inputs = _read_cell_inputs(
                    features_dir=features_dir, labels_dir=labels_dir,
                    stem=stem, cell=cell, beta=fits[cell]["beta"],
                )
                if inputs is None:
                    continue
                pooled.extend(
                    np.abs(inputs["predictions"][inputs["usable"]]).tolist()
                )
        deciles[cell] = discovery_decile_threshold(pooled)

    return {
        "frozen": frozen,
        "fits": fits,
        "deciles": deciles,
        "blocks": blocks,
        "confirmation": confirmation,
        "training": training,
        "by_date": by_date,
        "features_dir": features_dir,
        "labels_dir": labels_dir,
        "raw_dir": Path(args.raw_dir),
        "economic": economic,
        "batch_completeness": completeness,
    }


def _evaluate_block(
    context: dict[str, Any], session_dates: list[str], *, verify_hash: bool = True
):
    """Replay and evaluate every symbol-day in the given block."""

    from app.services.mbo_book_validator import MboBook, iter_dbn_events
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_executor import (
        BookReplay,
        common_factor_by_date,
        evaluate_symbol_day,
        make_sinks,
        query_instants,
        resolve_raw_source,
    )

    features_dir = context["features_dir"]
    sinks = make_sinks(float(FIXED_PRICE_SCALE))
    symbol_days: list[dict[str, Any]] = []
    factor_inputs: list[tuple[str, str, float]] = []

    stems = [s for d in session_dates for s in context["by_date"].get(d, [])]
    for index, stem in enumerate(sorted(stems), start=1):
        symbol, _, session_date = stem.rpartition("_")

        # (2) The raw file is the one Stage 1 recorded, verified byte-for-byte.
        manifest_path = features_dir / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"no Stage-1 manifest for {stem}; cannot bind raw input")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_path = resolve_raw_source(
            manifest, context["raw_dir"], stem=stem, verify_hash=verify_hash
        )

        print(f"[{index}/{len(stems)}] {stem}", flush=True)

        per_cell = []
        instants: set[int] = set()
        for cell in context["frozen"]["survivors"]:
            inputs = _read_cell_inputs(
                features_dir=features_dir, labels_dir=context["labels_dir"],
                stem=stem, cell=cell, beta=context["fits"][cell]["beta"],
            )
            if inputs is None:
                continue
            per_cell.append((cell, inputs))
            instants.update(
                query_instants(
                    inputs["decision"], inputs["availability"], inputs["usable"]
                )
            )
            if inputs["session_return_bps"] is not None and cell.startswith("50ev|"):
                factor_inputs.append(
                    (session_date, symbol, inputs["session_return_bps"])
                )

        if not per_cell:
            continue

        replay = BookReplay(MboBook)
        books = replay.run(iter_dbn_events(str(raw_path)), sorted(instants))

        for cell, inputs in per_cell:
            evaluate_symbol_day(
                symbol=symbol,
                session_date=session_date,
                design=inputs["design"],
                predictions=inputs["predictions"],
                decision_ts=inputs["decision"],
                exit_resolution_ts=inputs["availability"],
                usable=inputs["usable"],
                cell=cell,
                replay=replay,
                books=books,
                price_scale=float(FIXED_PRICE_SCALE),
                sinks=sinks,
                decile_threshold_bps=context["deciles"].get(cell),
            )
        symbol_days.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "raw_source": raw_path.name,
                "candidates": int(sum(int(i["usable"].sum()) for _c, i in per_cell)),
                "bad_ts_recv_instants": len(replay.bad_recv_instants),
            }
        )
        del books, replay, per_cell

    # Dedupe: one session return per (date, symbol).
    unique = {(d, s): v for d, s, v in factor_inputs}
    market = common_factor_by_date([(d, s, v) for (d, s), v in unique.items()])
    return sinks, symbol_days, market


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The economic pass, on the confirmation dates only.

    Gated: --i-have-reviewed-the-design is the reviewer's acknowledgement, not a
    default. There is no --limit here; a subset cannot produce a Stage-3
    economic result.
    """
    from app.services.mbo_stage3_executor import (
        assemble_report,
        summarize,
        write_report,
    )

    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-3 economic pass is not authorized yet. The design and "
            "implementation were to be reviewed first; re-run with "
            "--i-have-reviewed-the-design once that review has happened."
        )
    context = _prepare(args, economic=True)

    # (1) The fit was trained on discovery + validation. Economics are scored on
    # the confirmation dates and nowhere else.
    sinks, symbol_days, market = _evaluate_block(context, context["confirmation"])

    report = assemble_report(summarize(sinks, market), context["frozen"])
    report["evaluation"] = {
        "block": "confirmation",
        "session_dates": context["confirmation"],
        "fit_trained_on": context["training"],
        "economics_scored_on_training_dates": False,
    }
    report["batch_completeness"] = context["batch_completeness"]
    report["discovery_decile_thresholds_bps"] = context["deciles"]
    report["common_factor_by_date"] = market
    report["reproduction"] = {
        cell: {
            "alpha": fit["alpha"],
            "mean_delta_r2": fit["mean_delta_r2"],
            "reproduction_verified": fit["reproduction_verified"],
        }
        for cell, fit in context["fits"].items()
    }
    report["symbol_days"] = symbol_days
    write_report(report, Path(args.output_dir) / "stage3_results.json")
    return {k: v for k, v in report.items() if k not in ("cells", "symbol_days")}


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    """Feasibility only. Produces no economic result, by construction.

    A subset run cannot answer the primary question, so this command reports
    what the pipeline *found* -- provenance, candidate counts, why candidates
    did not become trades -- and never a return, a win rate or a verdict.
    """
    context = _prepare(args, economic=False)
    dates = context["confirmation"]
    if args.limit:
        dates = dates[: args.limit]
    sinks, symbol_days, _market = _evaluate_block(
        context, dates, verify_hash=not args.skip_hash_check
    )

    payload = {
        "stage3_plan_version": STAGE3_PLAN_VERSION,
        "stage3_plan_design_hash": PLAN_DESIGN_HASH,
        "diagnostic_only": True,
        "contains_economic_result": False,
        "why_no_result": (
            "a subset of the confirmation block cannot answer the primary "
            "question; this command reports feasibility and provenance only"
        ),
        "session_dates_examined": dates,
        "symbol_days": symbol_days,
        "discovery_decile_thresholds_bps": context["deciles"],
        "candidates_by_cell": {
            key[0]: sum(
                s.trades.__len__() + sum(s.no_trade_reasons.values())
                for k, s in sinks.items()
                if k[0] == key[0]
            )
            for key in sinks
        },
        "no_trade_reasons": {
            "|".join(key): dict(sorted(sink.no_trade_reasons.items()))
            for key, sink in sinks.items()
            if sink.no_trade_reasons
        },
        "trades_taken": {
            "|".join(key): len(sink.trades) for key, sink in sinks.items() if sink.trades
        },
    }
    _write(Path(args.output_dir) / "stage3_diagnostic.json", payload)
    return {k: v for k, v in payload.items() if k != "symbol_days"}


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

    def add_inputs(cmd):
        cmd.add_argument("--stage2-results", required=True)
        cmd.add_argument("--grams-dir", required=True)
        cmd.add_argument("--features-dir", required=True)
        cmd.add_argument("--labels-dir", required=True)
        cmd.add_argument("--raw-dir", required=True)

    run_cmd = subparsers.add_parser("run", help="The economic pass (gated).")
    add_inputs(run_cmd)
    # No --limit. A subset cannot produce a Stage-3 economic result, and an
    # option to try is an option to peek.
    run_cmd.add_argument("--i-have-reviewed-the-design", action="store_true")
    run_cmd.set_defaults(handler=run)

    diagnose_cmd = subparsers.add_parser(
        "diagnose",
        help="Feasibility and provenance only; produces no economic result.",
    )
    add_inputs(diagnose_cmd)
    diagnose_cmd.add_argument("--limit", type=int, default=0)
    diagnose_cmd.add_argument("--skip-hash-check", action="store_true")
    diagnose_cmd.set_defaults(handler=diagnose)
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
