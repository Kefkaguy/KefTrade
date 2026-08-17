"""Stage 1 feature extraction -- CLI.

Builds causal order-book state from reconstructed Tier-1 MBO files and writes
compact Parquet snapshots with manifests.

Stops before prediction. There is no forward return, no label, no Alpha Map,
no strategy and no threshold anywhere in this path.

    python -m app.cli.mbo_features definitions
    python -m app.cli.mbo_features file --path <file.dbn.zst>
    python -m app.cli.mbo_features batch --directory <dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_batch_validator import discover_files, parse_symbol_date
from app.services.mbo_book_validator import (
    assert_constants_match_databento,
    iter_dbn_events,
)
from app.services.mbo_feature_engine import (
    FEATURE_ENGINE_VERSION,
    FEATURE_VOCABULARY_HASH,
    feature_definitions,
)
from app.services.mbo_feature_store import (
    batch_manifest,
    estimate_storage,
    write_session_features,
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_mbo_features"


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def _guard_constants() -> None:
    checks = assert_constants_match_databento()
    mismatched = sorted(key for key, ok in checks.items() if not ok)
    if mismatched:
        raise ValueError(
            "local MBO constants disagree with the installed databento package: "
            f"{mismatched}. Refusing to build features against semantics that have moved."
        )


def definitions(args: argparse.Namespace) -> dict[str, Any]:
    payload = feature_definitions()
    _write(Path(args.output_dir) / "feature_definitions.json", payload)
    return payload


def _identity(path: Path) -> tuple[str, str]:
    symbol, date = parse_symbol_date(path.name)
    if symbol is None or date is None:
        raise ValueError(
            f"cannot determine symbol/session from {path.name!r}; refusing to write "
            "features that cannot be traced back to a symbol-day"
        )
    return symbol, date


def extract_file(args: argparse.Namespace) -> dict[str, Any]:
    _guard_constants()
    path = Path(args.path)
    if not path.is_file():
        raise ValueError(f"no such MBO file: {path}")
    symbol, session_date = _identity(path)
    output_dir = Path(args.output_dir)
    manifest = write_session_features(
        iter_dbn_events(str(path)),
        symbol=symbol,
        session_date=session_date,
        output_dir=output_dir,
        source_path=path,
    )
    return manifest


def extract_batch(args: argparse.Namespace) -> dict[str, Any]:
    _guard_constants()
    directory = Path(args.directory)
    if not directory.is_dir():
        raise ValueError(f"no such directory: {directory}")
    output_dir = Path(args.output_dir)
    files = discover_files(directory)
    if args.limit:
        files = files[: args.limit]

    manifests: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, path in enumerate(files, start=1):
        if not args.quiet:
            print(f"[{index}/{len(files)}] {path.name}", flush=True)
        try:
            symbol, session_date = _identity(path)
            manifests.append(
                write_session_features(
                    iter_dbn_events(str(path)),
                    symbol=symbol,
                    session_date=session_date,
                    output_dir=output_dir,
                    source_path=path,
                )
            )
        except Exception as error:  # noqa: BLE001 - one bad file must not end the walk
            failures.append({"source": path.name, "error": f"{type(error).__name__}: {error}"})
            if not args.quiet:
                print(f"    FAILED: {error}", flush=True)

    summary = batch_manifest(output_dir, manifests)
    summary["files_discovered"] = len(files)
    summary["files_completed"] = len(manifests)
    summary["files_failed"] = len(failures)
    summary["failures"] = failures
    summary["storage_estimate"] = estimate_storage(
        symbol_days=len(manifests),
        observed_rows=summary["total_rows"],
        observed_bytes=summary["total_bytes"],
    )
    _write(output_dir / "batch_manifest.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-features",
        description="Stage 1 causal order-book feature engine (no prediction).",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    definitions_parser = subparsers.add_parser(
        "definitions", help="Emit the frozen feature vocabulary and its hash."
    )
    definitions_parser.set_defaults(handler=definitions)

    file_parser = subparsers.add_parser("file", help="Extract features for one file.")
    file_parser.add_argument("--path", required=True)
    file_parser.set_defaults(handler=extract_file)

    batch_parser = subparsers.add_parser(
        "batch", help="Extract features for a directory of files."
    )
    batch_parser.add_argument("--directory", required=True)
    batch_parser.add_argument("--limit", type=int, default=0)
    batch_parser.add_argument("--quiet", action="store_true")
    batch_parser.set_defaults(handler=extract_batch)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(
        args.handler,
        args,
        banner=f"{FEATURE_ENGINE_VERSION} :: {args.command} :: vocab {FEATURE_VOCABULARY_HASH[:12]}",
    )


if __name__ == "__main__":
    main()
