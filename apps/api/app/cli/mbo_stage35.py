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
from app.services.mbo_stage35_executor import (
    STAGE35_EXECUTOR_VERSION,
    assert_chronology_is_clean,
    assert_frozen_plan,
    chronology_map,
)
from app.services.mbo_stage35_plan import (
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


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    """Provenance and counts only.

    The payload is filtered through ``_strip_outcomes`` on the way out, so a
    field that could carry an execution saving cannot reach the artefact even if
    someone later adds one upstream.
    """
    context = _prepare(args)
    payload = {
        "stage35_plan_version": STAGE35_PLAN_VERSION,
        "stage35_plan_design_hash": PLAN_DESIGN_HASH,
        "diagnostic_only": True,
        "contains_execution_outcome": False,
        "why_no_outcome": (
            "this command reports provenance and counts; it is filtered so that "
            "no execution saving can appear in its artefact"
        ),
        "frozen_cells": context["frozen"]["survivors"],
        "batch_completeness": context["batch_completeness"],
        "blocks": {k: list(v) for k, v in context["blocks"].items() if k != "unassigned"},
        "per_session_date_training": context["chronology"],
        "session_date_count": len(context["session_dates"]),
        "no_date_trains_on_itself": True,
    }
    clean = _strip_outcomes(payload)
    _write(Path(args.output_dir) / "stage35_diagnostic.json", clean)
    return clean


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The execution-timing pass.

    Gated. Stage 3.5 was specified to stop after the design and implementation
    were produced for review, so the reviewer flag is an acknowledgement rather
    than a default.
    """
    assert_frozen_plan()
    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-3.5 execution-timing pass is not authorized yet. The "
            "design and implementation were to be reviewed first; re-run with "
            "--i-have-reviewed-the-design once that review has happened."
        )
    context = _prepare(args)
    raise NotImplementedError(
        "the execution-timing pass is implemented as reviewable components "
        "(training_dates_for, evaluate_pair, CellTiming, assemble_report) and is "
        "deliberately not joined into a single command until the design is "
        f"approved. Cells: {context['frozen']['survivors']}"
    )


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
