"""Tier-1 all-session reconstruction / fidelity gate.

One question, and only one:

    Can all 160 frozen Tier-1 XNAS MBO symbol-days be reconstructed from a
    known starting state without structural data or book failures?

No feature, no imbalance, no microprice, no forward return, no label, no
predictive test, no Alpha Map cell, no P/L, no strategy, no economic threshold.
This module walks files, replays each through the *existing* per-file core, and
tabulates what came back.

Operating constraints, each for a reason:

* **Sequential.** 160 sessions of order-level data is far more than fits at
  once; the point of the exercise is a complete answer, not a fast one.
* **Streamed.** Files are read through ``iter_dbn_events``, which decompresses
  incrementally. Nothing is expanded to disk and no session is materialized.
* **Released between files.** Each book is dropped before the next file opens,
  so peak memory is one session rather than the batch.
* **No pandas.** The matrix is written row by row with ``csv``; loading 160
  reports into a dataframe to write a table would defeat the streaming above.
* **Failure does not stop the walk.** A file that cannot be read is recorded and
  the batch continues, because the useful output is *every* failing session, not
  the first one.
"""

from __future__ import annotations

import csv
import json
import re
import traceback
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from app.services.mbo_book_validator import (
    INIT_UNKNOWN,
    KNOWN_ACTIONS,
    MBO_VALIDATOR_VERSION,
    NON_FATAL_VIOLATIONS,
    VIOLATION_KINDS,
    iter_dbn_events,
    replay,
    validation_report,
)

MBO_BATCH_VERSION = "tier1_mbo_batch_gate_v1"

# The frozen experiment: batch XNAS-20260816-LYG4BEYSTM, 160 MBO symbol-days.
# Declared here so a short walk is a failure rather than a quietly smaller
# denominator.
EXPECTED_MBO_FILE_COUNT = 160

DBN_GLOB = "*.dbn.zst"

# xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst
_FILENAME = re.compile(
    r"^(?P<dataset>[a-z0-9-]+)-(?P<date>\d{8})\.(?P<schema>\w+)\.(?P<symbol>[A-Z0-9.\-]+)\."
    r"(?P<part>\d+)\.dbn\.zst$",
    re.IGNORECASE,
)

ACTION_COLUMNS = tuple(f"action_{action}" for action in sorted(KNOWN_ACTIONS))

MATRIX_COLUMNS: tuple[str, ...] = (
    "source",
    "symbol",
    "date",
    "records",
    "book_states_checked",
    "initialization_mode",
    "initialization_certified",
    "certified",
    "clean",
    "f_maybe_bad_book_records",
    "f_bad_ts_recv_live_records",
    "crossed_book_events",
    "locked_book_events",
    "sequence_regressions",
    "ts_event_regressions",
    "unknown_order_cancel",
    "unknown_order_fill",
    "unknown_order_modify",
    "duplicate_order_add",
    "cancel_exceeds_resting_size",
    "negative_or_undefined_size",
    "unknown_action",
    *ACTION_COLUMNS,
    "read_error",
)


def parse_symbol_date(name: str) -> tuple[str | None, str | None]:
    """Pull symbol and ISO date out of a Databento batch filename.

    Returns ``(None, None)`` rather than guessing when the name does not match:
    an unparsed row should be visible in the matrix, not silently attributed to
    the wrong symbol-day.
    """
    match = _FILENAME.match(name)
    if not match:
        return None, None
    raw_date = match.group("date")
    iso = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return match.group("symbol").upper(), iso


def discover_files(directory: Path, *, pattern: str = DBN_GLOB) -> list[Path]:
    """Every DBN file under ``directory``, in a stable order."""
    return sorted(path for path in directory.rglob(pattern) if path.is_file())


def matrix_row(report: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Flatten one per-file report into a single matrix row."""
    counts = report["integrity"]["violation_counts"]
    by_action = report["replay"]["by_action"]
    symbol, date = parse_symbol_date(source)
    return {
        "source": source,
        "symbol": symbol,
        "date": date,
        "records": report["replay"]["records"],
        "book_states_checked": report["flags"]["book_states_checked"],
        "initialization_mode": report["initialization"]["mode"],
        "initialization_certified": report["initialization"]["certified"],
        "certified": report["integrity"]["certified"],
        "clean": report["integrity"]["clean"],
        "f_maybe_bad_book_records": report["flags"]["f_maybe_bad_book_records"],
        "f_bad_ts_recv_live_records": report["flags"]["f_bad_ts_recv_live_records"],
        "crossed_book_events": report["integrity"]["crossed_book_events"],
        "locked_book_events": report["integrity"]["locked_book_events"],
        "sequence_regressions": counts["sequence_regression"],
        "ts_event_regressions": counts["ts_event_regression"],
        "unknown_order_cancel": counts["unknown_order_cancel"],
        "unknown_order_fill": counts["unknown_order_fill"],
        "unknown_order_modify": counts["unknown_order_modify"],
        "duplicate_order_add": counts["duplicate_order_add"],
        "cancel_exceeds_resting_size": counts["cancel_exceeds_resting_size"],
        "negative_or_undefined_size": counts["negative_or_undefined_size"],
        "unknown_action": counts["unknown_action"],
        **{f"action_{action}": by_action.get(action, 0) for action in sorted(KNOWN_ACTIONS)},
        "read_error": None,
    }


def failed_row(source: str, error: str) -> dict[str, Any]:
    """A file that could not be read still occupies a row.

    Omitting it would shrink the denominator and let a batch with unreadable
    sessions look complete.
    """
    row = dict.fromkeys(MATRIX_COLUMNS)
    row["source"] = source
    symbol, date = parse_symbol_date(source)
    row["symbol"] = symbol
    row["date"] = date
    row["initialization_mode"] = INIT_UNKNOWN
    row["initialization_certified"] = False
    row["certified"] = False
    row["clean"] = False
    row["read_error"] = error
    return row


def validate_one(path: Path, *, depth_levels: int = 10) -> dict[str, Any]:
    """Replay one file and drop its book before returning.

    The book, replay state and violation samples all go out of scope here, so
    the caller holds a report dict and nothing else.
    """
    book, state, violations = replay(iter_dbn_events(str(path)))
    report = validation_report(
        book, state, violations, source=path.name, depth_levels=depth_levels
    )
    del book, state, violations
    return report


def _fatal_total(counts: dict[str, int]) -> int:
    return sum(
        count
        for kind, count in counts.items()
        if kind not in NON_FATAL_VIOLATIONS and count
    )


def aggregate(
    rows: Sequence[dict[str, Any]],
    *,
    files_discovered: int,
    expected_file_count: int = EXPECTED_MBO_FILE_COUNT,
    fatal_totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    """The gate.

    ``overall_certified`` is a conjunction, deliberately: every clause has to
    hold, and any one of them failing is enough. A batch that is 159/160 clean
    is not 99% certified, it is uncertified with one session to look at.
    """
    completed = [row for row in rows if row["read_error"] is None]
    failed = [row for row in rows if row["read_error"] is not None]
    init_uncertified = [row for row in completed if not row["initialization_certified"]]
    integrity_uncertified = [row for row in completed if not row["certified"]]
    not_clean = [row for row in completed if not row["clean"]]

    total_maybe_bad_book = sum(row["f_maybe_bad_book_records"] or 0 for row in completed)
    total_bad_ts_live = sum(row["f_bad_ts_recv_live_records"] or 0 for row in completed)
    total_fatal = sum((fatal_totals or {}).values())

    checks = {
        "expected_file_count_met": len(completed) == expected_file_count,
        "no_unreadable_files": not failed,
        "all_initializations_certified": not init_uncertified,
        "all_integrity_certified": not integrity_uncertified,
        "all_clean": not not_clean,
        "no_maybe_bad_book": total_maybe_bad_book == 0,
        "no_fatal_violations": total_fatal == 0,
    }

    return {
        "batch_version": MBO_BATCH_VERSION,
        "validator_version": MBO_VALIDATOR_VERSION,
        "expected_file_count": expected_file_count,
        "files_discovered": files_discovered,
        "files_completed": len(completed),
        "files_failed_to_read": len(failed),
        "files_initialization_uncertified": len(init_uncertified),
        "files_integrity_uncertified": len(integrity_uncertified),
        "files_not_clean": len(not_clean),
        "total_records": sum(row["records"] or 0 for row in completed),
        "total_book_states_checked": sum(
            row["book_states_checked"] or 0 for row in completed
        ),
        "total_f_maybe_bad_book_records": total_maybe_bad_book,
        # Reported, never gating: a receive-clock problem is not a book defect.
        "total_f_bad_ts_recv_live_records": total_bad_ts_live,
        "total_fatal_violations": total_fatal,
        "fatal_violations_by_kind": dict(sorted((fatal_totals or {}).items())),
        "gate_checks": checks,
        "overall_certified": all(checks.values()),
        "failing_files": {
            "unreadable": [row["source"] for row in failed],
            "initialization_uncertified": [row["source"] for row in init_uncertified],
            "integrity_uncertified": [row["source"] for row in integrity_uncertified],
            "not_clean": [row["source"] for row in not_clean],
        },
    }


def write_matrix_csv(rows: Iterable[dict[str, Any]], path: Path) -> None:
    """Stream the matrix out row by row. No dataframe, no full materialization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MATRIX_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in MATRIX_COLUMNS})


def run_batch(
    directory: Path,
    *,
    output_dir: Path,
    expected_file_count: int = EXPECTED_MBO_FILE_COUNT,
    depth_levels: int = 10,
    pattern: str = DBN_GLOB,
    write_per_file_json: bool = True,
    progress: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Walk a directory of DBN files and produce the gate result.

    Returns ``(aggregate_report, matrix_rows)``. Always completes the walk: a
    file that raises is recorded and the next one is opened.
    """
    files = discover_files(directory, pattern=pattern)
    per_file_dir = output_dir / "per_file"
    rows: list[dict[str, Any]] = []
    fatal_totals: dict[str, int] = dict.fromkeys(VIOLATION_KINDS, 0)

    for index, path in enumerate(files, start=1):
        if progress is not None:
            progress(index, len(files), path.name)
        try:
            report = validate_one(path, depth_levels=depth_levels)
        except Exception as error:  # noqa: BLE001 - one bad file must not end the walk
            rows.append(
                failed_row(path.name, f"{type(error).__name__}: {error}".strip())
            )
            if progress is not None:
                progress(index, len(files), f"{path.name} FAILED: {error}")
            # The traceback holds frame locals, which on a mid-replay failure
            # includes the partly-built book. Dropping them keeps one bad file
            # from pinning a session's memory for the rest of the walk.
            if error.__traceback__ is not None:
                traceback.clear_frames(error.__traceback__)
            continue

        if write_per_file_json:
            per_file_dir.mkdir(parents=True, exist_ok=True)
            (per_file_dir / f"{path.stem}.validation.json").write_text(
                json.dumps(report, default=str, indent=2), encoding="utf-8"
            )
        counts = report["integrity"]["violation_counts"]
        for kind, count in counts.items():
            if kind not in NON_FATAL_VIOLATIONS and count:
                fatal_totals[kind] = fatal_totals.get(kind, 0) + count
        rows.append(matrix_row(report, source=path.name))
        # The report is retained; everything it was built from is not.
        del report

    fatal_present = {kind: count for kind, count in fatal_totals.items() if count}
    summary = aggregate(
        rows,
        files_discovered=len(files),
        expected_file_count=expected_file_count,
        fatal_totals=fatal_present,
    )
    summary["directory"] = str(directory)
    return summary, rows

