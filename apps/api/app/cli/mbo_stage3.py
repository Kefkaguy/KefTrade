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
        expected_count=args.expect_survivors or None,
    )
    frozen["stage3_plan_design_hash"] = PLAN_DESIGN_HASH
    frozen["contains_economic_result"] = False
    _write(Path(args.output_dir) / "stage3_frozen_survivors.json", frozen)
    return frozen


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The economic pass.

    Deliberately gated: Stage 3 was specified to stop after the design and
    implementation were produced for review. Passing --i-have-reviewed-the-design
    is the reviewer's acknowledgement, not a default.
    """
    assert_frozen_plan()
    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-3 economic pass is not authorized yet. The design and "
            "implementation were to be reviewed first; re-run with "
            "--i-have-reviewed-the-design once that review has happened."
        )
    frozen = load_frozen_survivors(
        Path(args.stage2_results), expected_count=args.expect_survivors or None
    )
    raise NotImplementedError(
        "the economic pass is implemented as reviewable components "
        "(BookReplay, evaluate_candidate, CellEconomics, assemble_report) but is "
        "deliberately not wired into a single command until the design is "
        f"approved. Frozen survivors: {frozen['survivors']}"
    )


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
    run_cmd.add_argument("--raw-dir", required=True)
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
