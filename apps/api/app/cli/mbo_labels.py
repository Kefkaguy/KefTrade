"""Stage 2A v2 -- CLI: event-time forward labels and the frozen Stage-2 plan.

Labels are resolved from the certified XNAS MBO stream, one replay per
symbol-day, against the frozen Stage-1 v2 feature snapshots. Computes no
correlation, no information coefficient, no ranking, no threshold and no P/L.

    python -m app.cli.mbo_labels definitions
    python -m app.cli.mbo_labels plan
    python -m app.cli.mbo_labels build --features-dir <dir> --raw-dir <dir>
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_batch_validator import discover_files, parse_symbol_date
from app.services.mbo_book_validator import iter_dbn_events
from app.services.mbo_label_engine import (
    LABEL_COLUMNS,
    LABEL_DEFINITION_HASH,
    LABEL_ENGINE_VERSION,
    LABEL_SCHEMA_HASH,
    REQUIRED_FEATURE_ENGINE_VERSION,
    REQUIRED_FEATURE_SEMANTICS_HASH,
    label_definitions,
    label_status_summary,
    read_source_snapshots,
    resolve_symbol_day_labels,
)
from app.services.mbo_stage2_plan import PLAN_HASH, statistical_plan

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_stage2_labels"

_STRING_COLUMNS = {"symbol", "session_date", "cadence"}
_FLOAT_SUFFIXES = ("_future_midpoint", "_midpoint_change", "_return_bps")


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def label_schema():
    """Wide schema: strings for identifiers and statuses, floats for prices and
    returns, int64 for everything time-like."""
    import pyarrow as pa

    fields = []
    for column in LABEL_COLUMNS:
        if column in _STRING_COLUMNS or column.endswith("_status"):
            fields.append(pa.field(column, pa.string()))
        elif column == "source_midpoint" or column.endswith(_FLOAT_SUFFIXES):
            fields.append(pa.field(column, pa.float64()))
        else:
            fields.append(pa.field(column, pa.int64()))
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
    manifest_path = features_dir / "batch_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(
            f"no batch_manifest.json under {features_dir}; refusing to label a "
            "feature set with no provenance"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    definitions_block = manifest.get("definitions", {})
    if definitions_block.get("feature_engine_version") != REQUIRED_FEATURE_ENGINE_VERSION:
        raise ValueError(
            f"feature engine is not the frozen {REQUIRED_FEATURE_ENGINE_VERSION!r}"
        )
    if definitions_block.get("feature_semantics_hash") != REQUIRED_FEATURE_SEMANTICS_HASH:
        raise ValueError(
            "feature semantics hash does not match the frozen Stage-1 v2 artefact"
        )
    if not manifest.get("feature_semantics_consistent", True):
        raise ValueError("the feature extraction itself is not semantics-consistent")
    return manifest


def _feature_files_by_symbol_day(features_dir: Path) -> dict[tuple[str, str], list[Path]]:
    """Group the frozen cadence files by (symbol, session_date)."""
    grouped: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for path in sorted(features_dir.rglob("*.parquet")):
        # <SYMBOL>_<YYYY-MM-DD>.<cadence>.parquet
        stem = path.name.split(".")[0]
        symbol, _, session_date = stem.rpartition("_")
        if not symbol or not session_date:
            raise ValueError(f"cannot parse symbol/session from {path.name!r}")
        grouped[(symbol, session_date)].append(path)
    return dict(grouped)


def _raw_files_by_symbol_day(raw_dir: Path) -> dict[tuple[str, str], Path]:
    mapping: dict[tuple[str, str], Path] = {}
    for path in discover_files(raw_dir):
        symbol, session_date = parse_symbol_date(path.name)
        if symbol is None or session_date is None:
            continue
        mapping[(symbol, session_date)] = path
    return mapping


def build(args: argparse.Namespace) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    features_dir = Path(args.features_dir)
    raw_dir = Path(args.raw_dir)
    if not features_dir.is_dir():
        raise ValueError(f"no such features directory: {features_dir}")
    if not raw_dir.is_dir():
        raise ValueError(f"no such raw MBO directory: {raw_dir}")
    feature_manifest = _assert_frozen_features(features_dir)

    output_dir = Path(args.output_dir)
    schema = label_schema()
    features = _feature_files_by_symbol_day(features_dir)
    raws = _raw_files_by_symbol_day(raw_dir)

    keys = sorted(features)
    if args.limit:
        keys = keys[: args.limit]

    per_symbol_day: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, key in enumerate(keys, start=1):
        symbol, session_date = key
        if not args.quiet:
            print(f"[{index}/{len(keys)}] {symbol} {session_date}", flush=True)
        raw_path = raws.get(key)
        if raw_path is None:
            failures.append(
                {
                    "symbol_day": f"{symbol} {session_date}",
                    "error": "no raw MBO file; labels require the certified stream",
                }
            )
            continue
        try:
            resolved_symbol, resolved_date, sources = read_source_snapshots(
                [str(p) for p in features[key]]
            )
            rows = resolve_symbol_day_labels(
                symbol=resolved_symbol,
                session_date=resolved_date,
                sources=sources,
                events=iter_dbn_events(str(raw_path)),
            )
            target = output_dir / f"{symbol}_{session_date}.labels.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            columns = {
                field.name: pa.array(
                    [row.get(field.name) for row in rows], type=field.type
                )
                for field in schema
            }
            pq.write_table(
                pa.Table.from_pydict(columns, schema=schema), target, compression="zstd"
            )
            summary = label_status_summary(rows)
            summary.update(
                {
                    "symbol": symbol,
                    "session_date": session_date,
                    "source_snapshots": len(sources),
                    "raw_source": raw_path.name,
                    "path": str(target.relative_to(output_dir)),
                    "bytes": target.stat().st_size,
                }
            )
            per_symbol_day.append(summary)
            del rows, sources
        except Exception as error:  # noqa: BLE001 - one bad symbol-day must not end the walk
            failures.append(
                {
                    "symbol_day": f"{symbol} {session_date}",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            if not args.quiet:
                print(f"    FAILED: {error}", flush=True)

    manifest = {
        "label_engine_version": LABEL_ENGINE_VERSION,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "stage2_plan_hash": PLAN_HASH,
        "label_source": "certified XNAS MBO stream",
        "built_against_feature_manifest": {
            "feature_engine_version": REQUIRED_FEATURE_ENGINE_VERSION,
            "feature_semantics_hash": REQUIRED_FEATURE_SEMANTICS_HASH,
            "symbol_days": feature_manifest.get("symbol_days"),
        },
        "symbol_days_discovered": len(features),
        "symbol_days_completed": len(per_symbol_day),
        "symbol_days_failed": len(failures),
        "failures": failures,
        "total_label_rows": sum(entry["rows"] for entry in per_symbol_day),
        "total_bytes": sum(entry["bytes"] for entry in per_symbol_day),
        "per_symbol_day": per_symbol_day,
        "features_modified": False,
        "contains_predictive_result": False,
    }
    _write(output_dir / "label_batch_manifest.json", manifest)
    return {k: v for k, v in manifest.items() if k != "per_symbol_day"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-labels",
        description="Stage 2A event-time forward labels and frozen statistical plan.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    definitions_parser = subparsers.add_parser(
        "definitions", help="Emit the frozen label definitions and their hashes."
    )
    definitions_parser.set_defaults(handler=definitions)

    plan_parser = subparsers.add_parser(
        "plan", help="Emit the frozen Stage-2 plan and multiplicity accounting."
    )
    plan_parser.set_defaults(handler=plan)

    build_cmd = subparsers.add_parser(
        "build", help="Resolve labels from the raw MBO stream for the frozen features."
    )
    build_cmd.add_argument("--features-dir", required=True)
    build_cmd.add_argument(
        "--raw-dir",
        required=True,
        help="directory of certified *.dbn.zst symbol-days; labels are event-time",
    )
    build_cmd.add_argument("--limit", type=int, default=0)
    build_cmd.add_argument("--quiet", action="store_true")
    build_cmd.set_defaults(handler=build)
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
