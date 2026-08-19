"""Stage 3.5 -- CLI: execution-timing mechanism study.

    python -m app.cli.mbo_stage35 plan
    python -m app.cli.mbo_stage35 chronology --grams-dir <dir>
    python -m app.cli.mbo_stage35 diagnose  --... (provenance and counts only)
    python -m app.cli.mbo_stage35 run       --... --i-have-reviewed-the-design

``plan`` and ``chronology`` compute no execution outcome. ``diagnose`` reports
provenance and counts and is structurally incapable of displaying a saving.
``run`` is gated.

Nothing here places an order. No broker client is importable from this module.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_stage3_executor import resolve_raw_source
from app.services.mbo_stage35_executor import (
    STAGE35_EXECUTOR_VERSION,
    assert_chronology_is_clean,
    assert_frozen_plan,
    chronology_map,
)
from app.services.mbo_stage35_plan import (
    EXCLUDED_LABEL_STATUSES,
    PLAN_DESIGN_HASH,
    STAGE35_PLAN_VERSION,
    statistical_plan,
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_stage35"

# Anything whose name could carry an execution outcome. The diagnostic payload
# is filtered against this rather than assembled carefully by hand, because
# "I remembered not to include it" is not a guarantee.
OUTCOME_BEARING = (
    "saving",
    "savings",
    "benefit",
    "fill",
    "midpoint",
    "dollar",
    "bps",
    "clustered_t",
    "p_value",
    "verdict",
    "passing",
)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def _strip_outcomes(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove anything that could be an execution outcome, by name, recursively."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if any(token in key.lower() for token in OUTCOME_BEARING):
            continue
        if isinstance(value, dict):
            clean[key] = _strip_outcomes(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            clean[key] = [_strip_outcomes(v) for v in value]
        else:
            clean[key] = value
    return clean


def plan(args: argparse.Namespace) -> dict[str, Any]:
    assert_frozen_plan()
    payload = statistical_plan()
    _write(Path(args.output_dir) / "stage35_plan.json", payload)
    return payload


def _blocks_from_grams(grams_dir: Path) -> dict[str, list[str]]:
    import numpy as np

    from app.services.mbo_stage2_executor import split_dates

    archive = np.load(grams_dir / "stage2_grams.npz")
    dates = sorted({key.split("||")[1] for key in archive.files if key.endswith("||xtx")})
    return split_dates(dates)


def chronology(args: argparse.Namespace) -> dict[str, Any]:
    """Record and verify the training set behind every date. No outcomes."""
    assert_frozen_plan()
    blocks = _blocks_from_grams(Path(args.grams_dir))
    mapping = chronology_map(blocks)
    assert_chronology_is_clean(mapping)
    payload = {
        "stage35_plan_version": STAGE35_PLAN_VERSION,
        "stage35_plan_design_hash": PLAN_DESIGN_HASH,
        "contains_execution_outcome": False,
        "blocks": {k: list(v) for k, v in blocks.items() if k != "unassigned"},
        "per_session_date": mapping,
        "no_date_trains_on_itself": True,
    }
    _write(Path(args.output_dir) / "stage35_chronology.json", payload)
    return payload


def _load_grams(grams_dir: Path):
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
    raise ValueError(f"no Stage-2 record for cell {cell!r}")


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Every Stage-3 provenance gate, unchanged, plus the Stage-3.5 chronology."""
    from app.services.mbo_stage3_executor import (
        assert_batch_complete,
        assert_feature_batch_is_frozen,
        load_frozen_survivors,
    )
    from app.services.mbo_stage3_plan import assert_session_dates_covered

    assert_frozen_plan()
    features_dir = Path(args.features_dir)
    labels_dir = Path(args.labels_dir)
    grams_dir = Path(args.grams_dir)

    assert_feature_batch_is_frozen(
        json.loads((features_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    )
    stage2 = json.loads(Path(args.stage2_results).read_text(encoding="utf-8"))
    frozen = load_frozen_survivors(Path(args.stage2_results), expected_count=4)
    completeness = assert_batch_complete(
        features_dir=features_dir,
        labels_dir=labels_dir,
        grams_dir=grams_dir,
        stage2_results=stage2,
    )

    blocks = _blocks_from_grams(grams_dir)
    session_dates = (
        list(blocks["discovery"]) + list(blocks["validation"]) + list(blocks["confirmation"])
    )
    assert_session_dates_covered(session_dates)
    mapping = chronology_map(blocks)
    assert_chronology_is_clean(mapping)

    return {
        "frozen": frozen,
        "batch_completeness": completeness,
        "blocks": blocks,
        "chronology": mapping,
        "session_dates": session_dates,
        "features_dir": features_dir,
        "labels_dir": labels_dir,
        "grams_dir": grams_dir,
        "raw_dir": Path(args.raw_dir),
        "stage2": stage2,
    }


def _build_fits(context: dict[str, Any]) -> dict[str, Any]:
    """Per-date betas for every cell, each proved to be Stage 2's own fit."""
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams = _load_grams(context["grams_dir"])
    fits: dict[str, Any] = {}
    reproduction: dict[str, Any] = {}
    for cell in context["frozen"]["survivors"]:
        record = _stage2_cell_record(context["stage2"], cell)
        alpha = record["chosen_alpha"]
        betas = per_date_betas(grams[cell], context["blocks"], alpha)
        recorded = recorded_stage2_per_date(record, context["blocks"])
        reproduction[cell] = reproduce_stage2_delta_r2(
            cell, grams[cell], betas, alpha, recorded
        )
        reproduction[cell]["alpha"] = alpha
        fits[cell] = betas
    return {"fits": fits, "reproduction": reproduction}


def _read_cell_inputs(
    *, features_dir: Path, labels_dir: Path, stem: str, cell: str, beta
):
    """Design, predictions, decision instants and target resolutions for one cell.

    The design matrix is built by the Stage-2 loader itself, so the features
    Stage 3.5 predicts on are the same objects Stage 2 fitted on rather than a
    re-implementation that could drift apart.
    """
    import numpy as np
    import pyarrow.parquet as pq

    from app.cli.mbo_stage2 import FEATURE_NAMES, _symbol_day_matrix
    from app.services.mbo_stage3_executor import (
        certify_spine,
        event_horizon_availability,
        predict,
    )
    from app.services.mbo_stage35_executor import cell_prefix, execution_eligibility

    cadence, horizon = cell.split("|")
    path = features_dir / cadence / f"{stem}.{cadence}.parquet"
    if not path.is_file():
        return None

    table = pq.read_table(
        path,
        columns=["sequence_index", "ts_event", "feature_available_ts_recv", *FEATURE_NAMES],
    )
    design, sequence = _symbol_day_matrix(table, FEATURE_NAMES)
    decision = np.asarray(
        table.column("feature_available_ts_recv").to_numpy(zero_copy_only=False), np.int64
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
    availability = event_horizon_availability(
        status, labels.column(f"{prefix}_available_ts_recv").filter(mask)
    )
    finite = np.isfinite(design).all(axis=1)
    # A target that never resolves is not a discarded row: it is precisely the
    # case the frozen policy covers by sending at the deadline. Filtering those
    # out would remove the quiet periods specifically.
    usable, status_counts = execution_eligibility(status, finite)
    return {
        "predictions": predict(np.nan_to_num(design, nan=0.0), beta),
        "decision": decision,
        "availability": availability,
        "usable": usable,
        "label_status_counts": status_counts,
    }


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    """Exercise every integrity gate the run depends on, and compute no outcome.

    A diagnostic that only checks provenance would report "clean" and then let
    the real run fail on the two gates that matter most -- the 20-date Stage-2
    reproduction and the label-status eligibility path. So this walks the same
    inputs the run walks, through the same functions, and stops short of
    evaluating a single execution pair.

    What it deliberately does not do: call ``evaluate_pair``, summarise a
    ``CellTiming``, assemble a report, or replay a book. Raw sources are bound
    and hashed but never opened for replay. The payload is filtered through
    ``_strip_outcomes`` on the way out, so a savings-bearing field could not
    reach the artefact even if one were added upstream.
    """
    context = _prepare(args)

    # Exercises the per-cell 10 + 6 + 4 = 20 date reproduction gate. These are
    # Stage-2 prediction-reproduction diagnostics, not Stage-3.5 execution
    # outcomes: they say the models are the ones Stage 2 fitted, nothing about
    # what timing them would earn.
    built = _build_fits(context)
    fits, reproduction = built["fits"], built["reproduction"]

    features_dir = context["features_dir"]
    stems = sorted({p.name.split(".")[0] for p in features_dir.rglob("*.parquet")})

    label_statuses: dict[str, dict[str, int]] = {}
    eligible_rows: dict[str, int] = {}
    excluded_rows: dict[str, dict[str, int]] = {}
    spine_certified_files = 0
    raw_sources_verified = 0
    inspected: list[dict[str, Any]] = []

    for index, stem in enumerate(sorted(stems), start=1):
        symbol, _, session_date = stem.rpartition("_")

        # Bind the exact Stage-1 bytes -- filename, size and SHA-256 -- without
        # replaying them. This is the file the run would consume.
        manifest_path = features_dir / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"no Stage-1 manifest for {stem}; cannot bind raw input")
        raw_path = resolve_raw_source(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            context["raw_dir"],
            stem=stem,
        )
        raw_sources_verified += 1

        print(f"[{index}/{len(stems)}] {stem}", flush=True)

        cells_seen = 0
        for cell in context["frozen"]["survivors"]:
            entry = fits[cell].get(session_date)
            if entry is None:
                continue
            # The real loader: full spine certification, nullable availability,
            # and the frozen eligibility rule including unknown-status refusal.
            inputs = _read_cell_inputs(
                features_dir=features_dir,
                labels_dir=context["labels_dir"],
                stem=stem,
                cell=cell,
                beta=entry["beta"],
            )
            if inputs is None:
                continue
            spine_certified_files += 1
            cells_seen += 1

            bucket = label_statuses.setdefault(cell, {})
            for name, count in inputs["label_status_counts"].items():
                bucket[name] = bucket.get(name, 0) + count
            eligible = int(inputs["usable"].sum())
            eligible_rows[cell] = eligible_rows.get(cell, 0) + eligible
            excluded = excluded_rows.setdefault(cell, {})
            for name, count in inputs["label_status_counts"].items():
                if name in EXCLUDED_LABEL_STATUSES:
                    excluded[name] = excluded.get(name, 0) + count
            # inputs["predictions"] is deliberately not read, recorded or
            # returned: a prediction is not an outcome, but it is not a count
            # either, and this artefact carries counts.

        inspected.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "raw_source": raw_path.name,
                "raw_source_verified": True,
                "cells_inspected": cells_seen,
            }
        )

    payload = {
        "stage35_plan_version": STAGE35_PLAN_VERSION,
        "stage35_plan_design_hash": PLAN_DESIGN_HASH,
        "stage35_executor_version": STAGE35_EXECUTOR_VERSION,
        "diagnostic_only": True,
        "contains_execution_outcome": False,
        "why_no_outcome": (
            "this command exercises the integrity gates the run depends on and "
            "stops before evaluating a single execution pair"
        ),
        "frozen_cells": context["frozen"]["survivors"],
        "frozen_cell_count": len(context["frozen"]["survivors"]),
        "batch_completeness": context["batch_completeness"],
        "symbol_days_inspected": len(inspected),
        "session_date_count": len(context["session_dates"]),
        "raw_sources_verified": raw_sources_verified,
        "spine_certified_cell_files": spine_certified_files,
        "source_label_status_counts": label_statuses,
        "eligible_rows_by_cell": eligible_rows,
        "excluded_rows_by_cell_and_status": excluded_rows,
        "stage2_reproduction": reproduction,
        "blocks": {k: list(v) for k, v in context["blocks"].items() if k != "unassigned"},
        "per_session_date_training": context["chronology"],
        "no_date_trains_on_itself": True,
        "symbol_days": inspected,
    }
    clean = _strip_outcomes(payload)
    _write(Path(args.output_dir) / "stage35_diagnostic.json", clean)
    return {k: v for k, v in clean.items() if k != "symbol_days"}


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The execution-timing pass.

    Gated: the reviewer flag is an acknowledgement, not a default.
    """

    from app.services.mbo_book_validator import MboBook, iter_dbn_events
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_executor import BookReplay
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE
    from app.services.mbo_stage35_executor import (
        CellTiming,
        CoverageTracker,
        assemble_report,
        evaluate_pair,
        query_instants,
        write_report,
    )

    assert_frozen_plan()
    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-3.5 execution-timing pass is not authorized yet. The "
            "design and implementation were to be reviewed first; re-run with "
            "--i-have-reviewed-the-design once that review has happened."
        )

    context = _prepare(args)
    built = _build_fits(context)
    fits, reproduction = built["fits"], built["reproduction"]
    price_scale = float(FIXED_PRICE_SCALE)

    sinks = {
        cell: CellTiming(cell=cell, price_scale=price_scale)
        for cell in context["frozen"]["survivors"]
    }
    status_counts: dict[str, dict[str, int]] = {}
    features_dir = context["features_dir"]
    stems = sorted({p.name.split(".")[0] for p in features_dir.rglob("*.parquet")})
    symbol_days: list[dict[str, Any]] = []

    for index, stem in enumerate(stems, start=1):
        symbol, _, session_date = stem.rpartition("_")
        manifest_path = features_dir / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"no Stage-1 manifest for {stem}; cannot bind raw input")
        raw_path = resolve_raw_source(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            context["raw_dir"],
            stem=stem,
        )
        print(f"[{index}/{len(stems)}] {stem}", flush=True)

        per_cell = []
        instants: set[int] = set()
        for cell in context["frozen"]["survivors"]:
            entry = fits[cell].get(session_date)
            if entry is None:
                continue
            inputs = _read_cell_inputs(
                features_dir=features_dir,
                labels_dir=context["labels_dir"],
                stem=stem,
                cell=cell,
                beta=entry["beta"],
            )
            if inputs is None:
                continue
            per_cell.append((cell, entry["block"], inputs))
            bucket = status_counts.setdefault(cell, {})
            for name, count in inputs["label_status_counts"].items():
                bucket[name] = bucket.get(name, 0) + count
            instants.update(
                query_instants(
                    inputs["decision"], inputs["availability"],
                    inputs["usable"], inputs["predictions"],
                )
            )

        if not per_cell:
            continue

        coverage = CoverageTracker()
        replay = BookReplay(MboBook)
        books = replay.run(
            coverage.wrap(iter_dbn_events(str(raw_path))), sorted(instants)
        )

        # Bound by value, not captured by name: `books` is released at the end
        # of each iteration, and a closure over the name would go stale.
        def book_at(ts: int, _books=books):
            return _books.get(ts)

        for cell, block, inputs in per_cell:
            sink = sinks[cell]
            usable = inputs["usable"]
            for row in range(len(inputs["predictions"])):
                if not usable[row]:
                    continue
                pair, reason = evaluate_pair(
                    cell=cell,
                    symbol=symbol,
                    session_date=session_date,
                    block=block,
                    predicted_bps=float(inputs["predictions"][row]),
                    decision_ts=int(inputs["decision"][row]),
                    target_available_ts_recv=inputs["availability"][row],
                    book_at=book_at,
                    price_scale=price_scale,
                    timing_certified=replay.timing_certified,
                    within_coverage=coverage.covers,
                )
                if pair is None:
                    sink.record_not_comparable(reason or "unknown")
                else:
                    sink.pairs.append(pair)

        symbol_days.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "raw_source": raw_path.name,
                "coverage": coverage.as_dict(),
                "bad_ts_recv_instants": len(replay.bad_recv_instants),
            }
        )
        del books, replay, per_cell, book_at

    report = assemble_report(
        [sink.summary(PRIMARY_FEE_SCHEDULE) for sink in sinks.values()],
        chronology=context["chronology"],
    )
    report["source_label_status_counts"] = status_counts
    report["stage2_fit_reproduction"] = reproduction
    report["batch_completeness"] = context["batch_completeness"]
    report["symbol_days"] = symbol_days
    write_report(report, Path(args.output_dir) / "stage35_results.json")
    return {k: v for k, v in report.items() if k not in ("cells", "symbol_days")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-stage35",
        description="Stage 3.5: execution-timing mechanism study (not a strategy).",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_cmd = subparsers.add_parser("plan", help="Emit the frozen Stage-3.5 plan.")
    plan_cmd.set_defaults(handler=plan)

    chronology_cmd = subparsers.add_parser(
        "chronology", help="Record and verify the per-date training sets."
    )
    chronology_cmd.add_argument("--grams-dir", required=True)
    chronology_cmd.set_defaults(handler=chronology)

    def add_inputs(cmd):
        cmd.add_argument("--stage2-results", required=True)
        cmd.add_argument("--grams-dir", required=True)
        cmd.add_argument("--features-dir", required=True)
        cmd.add_argument("--labels-dir", required=True)
        cmd.add_argument("--raw-dir", required=True)

    diagnose_cmd = subparsers.add_parser(
        "diagnose", help="Provenance and counts only; cannot display savings."
    )
    add_inputs(diagnose_cmd)
    diagnose_cmd.set_defaults(handler=diagnose)

    run_cmd = subparsers.add_parser("run", help="The execution-timing pass (gated).")
    add_inputs(run_cmd)
    run_cmd.add_argument("--i-have-reviewed-the-design", action="store_true")
    run_cmd.set_defaults(handler=run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(
        args.handler,
        args,
        banner=f"{STAGE35_EXECUTOR_VERSION} :: {args.command} :: "
        f"{STAGE35_PLAN_VERSION} {PLAN_DESIGN_HASH[:12]}",
    )


if __name__ == "__main__":
    main()
