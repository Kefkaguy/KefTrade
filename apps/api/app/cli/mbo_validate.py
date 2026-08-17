"""Tier-1 MBO book reconstruction validator -- CLI.

Replays one Databento MBO file into an order-ID keyed book and reports whether
the reconstruction holds together.  Database-free, and incapable of testing a
hypothesis: it builds no feature, measures no forward return, and declares no
trial.

    python -m app.cli.mbo_validate file --path <file.dbn.zst>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_batch_validator import (
    EXPECTED_MBO_FILE_COUNT,
    run_batch,
    write_matrix_csv,
)
from app.services.mbo_book_validator import (
    MBO_VALIDATOR_VERSION,
    assert_constants_match_databento,
    validate_dbn_file,
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_mbo_validation"

# Distinct from the refusal code (2) so a failing gate is distinguishable from a
# governance refusal in a shell.
GATE_FAILED_EXIT_CODE = 3


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def validate_file(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.path)
    if not path.is_file():
        raise ValueError(f"no such MBO file: {path}")

    constants = assert_constants_match_databento()
    mismatched = sorted(key for key, ok in constants.items() if not ok)
    if mismatched:
        raise ValueError(
            "local MBO constants disagree with the installed databento package: "
            f"{mismatched}. Refusing to validate against semantics that have moved."
        )

    report = validate_dbn_file(str(path), depth_levels=args.depth_levels)
    report["constants_verified_against_databento"] = True
    report["source"] = path.name

    output_dir = Path(args.output_dir)
    _write(output_dir / f"{path.stem}.validation.json", report)

    if args.summary_only:
        return {
            "validator_version": report["validator_version"],
            "source": report["source"],
            "records": report["replay"]["records"],
            "by_action": report["replay"]["by_action"],
            "snapshot": report["snapshot"],
            "final_book": {
                key: value
                for key, value in report["final_book"].items()
                if key != "depth"
            },
            "integrity": {
                "clean": report["integrity"]["clean"],
                "fatal_violation_counts": report["integrity"]["fatal_violation_counts"],
                "crossed_book_events": report["integrity"]["crossed_book_events"],
                "locked_book_events": report["integrity"]["locked_book_events"],
            },
        }
    return report


def batch(args: argparse.Namespace) -> dict[str, Any]:
    """Walk a directory of DBN files and apply the Tier-1 fidelity gate.

    Completes the whole walk even when files fail, then exits non-zero if the
    gate did not pass -- so the operator gets every failing session in one run
    rather than discovering them one restart at a time.
    """
    directory = Path(args.directory)
    if not directory.is_dir():
        raise ValueError(f"no such directory: {directory}")

    constants_check = assert_constants_match_databento()
    mismatched = sorted(key for key, ok in constants_check.items() if not ok)
    if mismatched:
        raise ValueError(
            "local MBO constants disagree with the installed databento package: "
            f"{mismatched}. Refusing to validate against semantics that have moved."
        )

    output_dir = Path(args.output_dir)

    def progress(index: int, total: int, name: str) -> None:
        if not args.quiet:
            print(f"[{index}/{total}] {name}", flush=True)

    summary, rows = run_batch(
        directory,
        output_dir=output_dir,
        expected_file_count=args.expected_files,
        depth_levels=args.depth_levels,
        write_per_file_json=not args.no_per_file_json,
        progress=progress,
    )
    summary["constants_verified_against_databento"] = True

    write_matrix_csv(rows, output_dir / "tier1_mbo_matrix.csv")
    _write(output_dir / "tier1_mbo_aggregate.json", summary)

    # The report is written before the exit code is decided, so a failing gate
    # still leaves complete evidence behind.
    if not summary["overall_certified"]:
        print(json.dumps(summary, default=str, indent=2), flush=True)
        raise SystemExit(GATE_FAILED_EXIT_CODE)
    return summary


def constants(args: argparse.Namespace) -> dict[str, Any]:
    checks = assert_constants_match_databento()
    return {
        "validator_version": MBO_VALIDATOR_VERSION,
        "checks": checks,
        "all_match": all(checks.values()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-validate",
        description="Tier-1 MBO book reconstruction validator (no hypothesis testing).",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    file_parser = subparsers.add_parser("file", help="Validate one DBN MBO file.")
    file_parser.add_argument("--path", required=True)
    file_parser.add_argument("--depth-levels", type=int, default=10)
    file_parser.add_argument("--summary-only", action="store_true")
    file_parser.set_defaults(handler=validate_file)

    batch_parser = subparsers.add_parser(
        "batch", help="Validate a directory of DBN MBO files against the Tier-1 gate."
    )
    batch_parser.add_argument("--directory", required=True)
    batch_parser.add_argument(
        "--expected-files",
        type=int,
        default=EXPECTED_MBO_FILE_COUNT,
        help="Files that must complete for the gate to pass (default: the frozen 160).",
    )
    batch_parser.add_argument("--depth-levels", type=int, default=10)
    batch_parser.add_argument("--no-per-file-json", action="store_true")
    batch_parser.add_argument("--quiet", action="store_true")
    batch_parser.set_defaults(handler=batch)

    constants_parser = subparsers.add_parser(
        "constants", help="Check local MBO constants against the databento package."
    )
    constants_parser.set_defaults(handler=constants)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(args.handler, args, banner=f"{MBO_VALIDATOR_VERSION} :: {args.command}")


if __name__ == "__main__":
    main()
