"""Stage 2A -- CLI: forward labels and the frozen statistical plan.

Emits label definitions, the Stage-2 plan with its multiplicity accounting, and
builds labels against the frozen Stage-1 v2 Parquet dataset.

Computes no predictive result. There is no correlation, no information
coefficient, no ranking and no threshold anywhere in this path.

    python -m app.cli.mbo_labels definitions
    python -m app.cli.mbo_labels plan
    python -m app.cli.mbo_labels build --features-dir <dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_label_engine import (
    LABEL_COLUMNS,
    LABEL_DEFINITION_HASH,
    LABEL_ENGINE_VERSION,
    REQUIRED_FEATURE_ENGINE_VERSION,
    REQUIRED_FEATURE_SEMANTICS_HASH,
    build_labels,
    label_definitions,
    label_status_summary,
    read_spine,
)
from app.services.mbo_stage2_plan import PLAN_HASH, statistical_plan

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_stage2_labels"

_INT_COLUMNS = {
    "sequence_index",
    "horizon_magnitude",
    "source_ts_event",
    "source_grid_ts_event",
    "source_ts_recv",
    "source_feature_available_ts_recv",
    "target_ts_event",
    "label_sequence_index",
    "label_ts_event",
    "label_ts_recv",
    "realized_lag_ns",
    "skipped_incoherent_states",
    "label_available_ts_recv",
}
_STRING_COLUMNS = {"symbol", "session_date", "cadence", "horizon", "horizon_kind", "label_status"}


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def label_schema():
    import pyarrow as pa

    fields = []
    for column in LABEL_COLUMNS:
        if column in _STRING_COLUMNS:
            fields.append(pa.field(column, pa.string()))
        elif column in _INT_COLUMNS:
            fields.append(pa.field(column, pa.int64()))
        else:
            fields.append(pa.field(column, pa.float64()))
    return pa.schema(fields)


def definitions(args: argparse.Namespace) -> dict[str, Any]:
    payload = label_definitions()
    _write(Path(args.output_dir) / "label_definitions.json", payload)
    return payload


def plan(args: argparse.Namespace) -> dict[str, Any]:
    payload = statistical_plan()
    payload["plan_hash"] = PLAN_HASH
    _write(Path(args.output_dir) / "stage2_statistical_plan.json", payload)
    return payload


def _assert_frozen_features(features_dir: Path) -> dict[str, Any]:
    """Refuse to label a feature set that is not the frozen Stage-1 v2 artefact."""
    manifest_path = features_dir / "batch_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(
            f"no batch_manifest.json under {features_dir}; refusing to build labels "
            "against a feature set with no provenance"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    definitions_block = manifest.get("definitions", {})
    engine = definitions_block.get("feature_engine_version")
    semantics = definitions_block.get("feature_semantics_hash")
    if engine != REQUIRED_FEATURE_ENGINE_VERSION:
        raise ValueError(
            f"feature engine {engine!r} is not the frozen {REQUIRED_FEATURE_ENGINE_VERSION!r}"
        )
    if semantics != REQUIRED_FEATURE_SEMANTICS_HASH:
        raise ValueError(
            "feature semantics hash does not match the frozen Stage-1 v2 artefact; "
            "labels built against different semantics are not comparable"
        )
    if not manifest.get("feature_semantics_consistent", True):
        raise ValueError("the feature extraction itself is not semantics-consistent")
    return manifest


def build(args: argparse.Namespace) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    features_dir = Path(args.features_dir)
    if not features_dir.is_dir():
        raise ValueError(f"no such features directory: {features_dir}")
    feature_manifest = _assert_frozen_features(features_dir)

    output_dir = Path(args.output_dir)
    schema = label_schema()
    parquet_files = sorted(features_dir.rglob("*.parquet"))
    if args.limit:
        parquet_files = parquet_files[: args.limit]

    per_file: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, path in enumerate(parquet_files, start=1):
        if not args.quiet:
            print(f"[{index}/{len(parquet_files)}] {path.name}", flush=True)
        try:
            spine = read_spine(str(path))
            rows = list(build_labels(spine))
            target = (
                output_dir
                / spine.cadence
                / f"{spine.symbol}_{spine.session_date}.{spine.cadence}.labels.parquet"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            columns = {
                field.name: pa.array([row.get(field.name) for row in rows], type=field.type)
                for field in schema
            }
            pq.write_table(
                pa.Table.from_pydict(columns, schema=schema), target, compression="zstd"
            )
            summary = label_status_summary(rows)
            summary.update(
                {
                    "symbol": spine.symbol,
                    "session_date": spine.session_date,
                    "cadence": spine.cadence,
                    "snapshots": len(spine),
                    "path": str(target.relative_to(output_dir)),
                    "bytes": target.stat().st_size,
                }
            )
            per_file.append(summary)
            del rows, spine
        except Exception as error:  # noqa: BLE001 - one bad file must not end the walk
            failures.append({"source": path.name, "error": f"{type(error).__name__}: {error}"})
            if not args.quiet:
                print(f"    FAILED: {error}", flush=True)

    manifest = {
        "label_engine_version": LABEL_ENGINE_VERSION,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "stage2_plan_hash": PLAN_HASH,
        "built_against_feature_manifest": {
            "feature_engine_version": REQUIRED_FEATURE_ENGINE_VERSION,
            "feature_semantics_hash": REQUIRED_FEATURE_SEMANTICS_HASH,
            "symbol_days": feature_manifest.get("symbol_days"),
        },
        "files_discovered": len(parquet_files),
        "files_completed": len(per_file),
        "files_failed": len(failures),
        "failures": failures,
        "total_label_rows": sum(entry["rows"] for entry in per_file),
        "total_bytes": sum(entry["bytes"] for entry in per_file),
        "per_file": per_file,
        "features_modified": False,
        "contains_predictive_result": False,
    }
    _write(output_dir / "label_batch_manifest.json", manifest)
    return {key: value for key, value in manifest.items() if key != "per_file"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-labels",
        description="Stage 2A causal forward labels and frozen statistical plan.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    definitions_parser = subparsers.add_parser(
        "definitions", help="Emit the frozen label definitions and their hash."
    )
    definitions_parser.set_defaults(handler=definitions)

    plan_parser = subparsers.add_parser(
        "plan", help="Emit the frozen Stage-2 statistical plan and multiplicity count."
    )
    plan_parser.set_defaults(handler=plan)

    build_parser_ = subparsers.add_parser(
        "build", help="Build labels from the frozen Stage-1 v2 feature Parquet."
    )
    build_parser_.add_argument("--features-dir", required=True)
    build_parser_.add_argument("--limit", type=int, default=0)
    build_parser_.add_argument("--quiet", action="store_true")
    build_parser_.set_defaults(handler=build)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(
        args.handler,
        args,
        banner=f"{LABEL_ENGINE_VERSION} :: {args.command} :: labels {LABEL_DEFINITION_HASH[:12]}",
    )


if __name__ == "__main__":
    main()
