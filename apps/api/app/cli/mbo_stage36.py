"""Stage 3.6 -- CLI: news-triggered L3 consensus economic experiment.

    python -m app.cli.mbo_stage36 plan
    python -m app.cli.mbo_stage36 verify
    python -m app.cli.mbo_stage36 diagnose --...
    python -m app.cli.mbo_stage36 run      --... --i-have-reviewed-the-design

``plan`` and ``verify`` compute no economic outcome and need no market data.
``diagnose`` exercises every gate the run depends on and stops before a single
fill is priced. ``run`` is gated and takes no subset flag: the design freezes one
primary specification over the complete candidate population, and a subset of it
would be a different specification.

Nothing here places an order. No broker client is imported, and no code path
could reach one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.mbo_stage36_executor import (
    STAGE36_EXECUTOR_VERSION,
    assert_consensus_is_internally_consistent,
    assert_frozen_counts,
    assert_frozen_plan,
    assert_predictions_are_causal,
    load_candidates,
    verify_preoutcome_artifacts,
)
from app.services.mbo_stage36_plan import (
    EXPECTED_DESIGN_SHA256,
    EXPECTED_MANIFEST_SHA256,
    FROZEN_CELLS,
    PLAN_DESIGN_HASH,
    STAGE36_PLAN_VERSION,
    statistical_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "tier1_stage36_results"

# Any key whose name could carry a Stage-3.6 economic outcome. The diagnostic
# payload is filtered against this rather than assembled carefully by hand,
# because "I remembered not to include it" is not a guarantee.
OUTCOME_BEARING = (
    "return",
    "pnl",
    "p_and_l",
    "profit",
    "fill",
    "bps",
    "net_",
    "gross",
    "expectancy",
    "verdict",
    "clustered",
    "p_value",
    "savings",
    "execution_cost",
    "fee",
    "stretch",
    "supported",
)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def _strip_outcomes(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove anything that could be an economic outcome, by name, recursively."""
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


def _count_records(counts: dict[str, int]) -> list[dict[str, Any]]:
    """Counts as records, so a label is a value rather than a key.

    Consensus labels are safe as keys today, but serializing them as values
    keeps the payload immune to the filter regardless of how a label is later
    named.
    """
    return [{"label": label, "count": count} for label, count in sorted(counts.items())]


def plan(args: argparse.Namespace) -> dict[str, Any]:
    assert_frozen_plan(REPO_ROOT)
    payload = statistical_plan()
    _write(Path(args.output_dir) / "stage36_plan.json", payload)
    return payload


def _verify_population() -> dict[str, Any]:
    """Design, artefacts, counts, causality and consensus consistency.

    Needs no market data, so it runs anywhere the frozen artefacts are checked
    out. Every step refuses rather than reports on failure.
    """
    design = assert_frozen_plan(REPO_ROOT)
    artifacts = verify_preoutcome_artifacts(REPO_ROOT)
    candidates = load_candidates(REPO_ROOT)
    counts = assert_frozen_counts(candidates)
    causal = assert_predictions_are_causal(candidates)
    consistent = assert_consensus_is_internally_consistent(candidates)

    strong = [c for c in candidates if c.is_strong_consensus]
    timing = {
        "t0_to_td_seconds": 30,
        "entry_latency_ms": 250,
        "exit_request_after_td_seconds": 300,
        "arrival_to_arrival_seconds": 300,
        "verified_on_candidates": len(strong),
        "all_exact": all(
            c.td_ns - c.t0_ns == 30_000_000_000
            and c.entry_arrival_ns - c.td_ns == 250_000_000
            and c.exit_request_ns - c.td_ns == 300_000_000_000
            and c.exit_arrival_ns - c.entry_arrival_ns == 300_000_000_000
            for c in strong
        ),
    }
    if not timing["all_exact"]:
        raise ValueError("the frozen Stage-3.6 timing semantics do not hold exactly")

    return {
        "design": design,
        "artifacts": artifacts,
        "candidates": candidates,
        "counts": counts,
        "causal": causal,
        "consistent": consistent,
        "timing": timing,
        "strong": strong,
    }


def verify(args: argparse.Namespace) -> dict[str, Any]:
    """Artefact and population verification only. No market data, no outcome."""
    state = _verify_population()
    counts = state["counts"]
    candidates = state["candidates"]
    payload = {
        "stage36_plan_version": STAGE36_PLAN_VERSION,
        "stage36_executor_version": STAGE36_EXECUTOR_VERSION,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "design_sha256": EXPECTED_DESIGN_SHA256,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "artifact_only": True,
        "contains_economic_outcome": False,
        "frozen_cells": list(FROZEN_CELLS),
        "measured_events": counts["measured_events"],
        "consensus_counts": _count_records(
            {k: v for k, v in counts.items() if k not in ("measured_events",)}
        ),
        "predictions_checked": state["causal"]["predictions_checked"],
        "all_predictions_within_t0_to_td": state["causal"]["all_within_t0_to_td"],
        "consensus_labels_rederived": state["consistent"]["candidates_rederived"],
        "consensus_labels_consistent": state["consistent"]["labels_consistent"],
        "timing_semantics": state["timing"],
        "sessions_in_population": len({c.session_date for c in candidates}),
        "symbols_in_population": len({c.symbol for c in candidates}),
    }
    clean = _strip_outcomes(payload)
    _write(Path(args.output_dir) / "stage36_verify.json", clean)
    return clean


def context_raw_dir_exists(raw_dir: Path) -> bool:
    return raw_dir.is_dir()


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Every inherited Stage-3 provenance gate, plus the Stage-3.5 chronology."""
    from app.cli.mbo_stage35 import _blocks_from_grams
    from app.services.mbo_stage3_executor import (
        assert_batch_complete,
        assert_feature_batch_is_frozen,
        load_frozen_survivors,
    )
    from app.services.mbo_stage3_plan import assert_session_dates_covered
    from app.services.mbo_stage35_executor import (
        assert_chronology_is_clean,
        chronology_map,
    )

    features_dir = Path(args.features_dir)
    labels_dir = Path(args.labels_dir)
    grams_dir = Path(args.grams_dir)

    # A missing input is a refusal with a name attached, not a traceback: the
    # operator needs to know which artefact is absent, not where Python gave up.
    for label, path in (
        ("feature batch manifest", features_dir / "batch_manifest.json"),
        ("Stage-2 results", Path(args.stage2_results)),
        ("Stage-2 grams", grams_dir / "stage2_grams.npz"),
        ("label batch manifest", labels_dir / "label_batch_manifest.json"),
    ):
        if not path.is_file():
            raise ValueError(
                f"the {label} is missing at {path}. Stage 3.6 needs the complete "
                "certified Stage-1/Stage-2 batch; it will not proceed on a "
                "partial one."
            )
    if not context_raw_dir_exists(Path(args.raw_dir)):
        raise ValueError(
            f"the raw MBO directory is missing at {args.raw_dir}. Stage 3.6 binds "
            "every raw source by SHA-256 before replay."
        )

    assert_feature_batch_is_frozen(
        json.loads((features_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    )
    stage2 = json.loads(Path(args.stage2_results).read_text(encoding="utf-8"))
    frozen = load_frozen_survivors(Path(args.stage2_results), expected_count=4)
    if sorted(frozen["survivors"]) != sorted(FROZEN_CELLS):
        raise ValueError(
            "the Stage-2 survivors are not the four cells Stage 3.6 froze: "
            f"{frozen['survivors']} vs {list(FROZEN_CELLS)}"
        )
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
    """Stage-3.5's own per-date reconstruction, with its 20-date proof."""
    from app.cli.mbo_stage35 import _load_grams, _stage2_cell_record
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams = _load_grams(context["grams_dir"])
    fits: dict[str, Any] = {}
    reproduction: dict[str, Any] = {}
    for cell in FROZEN_CELLS:
        record = _stage2_cell_record(context["stage2"], cell)
        alpha = record["chosen_alpha"]
        betas = per_date_betas(grams[cell], context["blocks"], alpha)
        reproduction[cell] = reproduce_stage2_delta_r2(
            cell,
            grams[cell],
            betas,
            alpha,
            recorded_stage2_per_date(record, context["blocks"]),
        )
        reproduction[cell]["alpha"] = alpha
        fits[cell] = betas
    return {"fits": fits, "reproduction": reproduction}


def _recomputed_population(context: dict[str, Any], built: dict[str, Any], state: dict[str, Any]):
    """Regenerate every decision from the models, then require the census to match.

    The frozen CSV is a commitment, not a signal source. Reading a direction out
    of it would mean the experiment never actually ran the four models it claims
    to be testing. So the predictions are recomputed here from the frozen fits
    and the frozen feature parquet, and the census must then reproduce exactly --
    event by event, not merely in aggregate.
    """
    from app.services.mbo_stage36_executor import (
        recompute_consensus,
        reconcile_with_frozen_census,
    )

    frozen = state["candidates"]
    runtime = recompute_consensus(frozen, context["features_dir"], built["fits"])
    reconciliation = reconcile_with_frozen_census(runtime, frozen)
    return runtime, reconciliation


def _bind_raw_sources(context: dict[str, Any], stems: list[str]) -> list[dict[str, Any]]:
    """Resolve and hash-verify every raw source the run would consume."""
    from app.services.mbo_stage3_executor import resolve_raw_source

    features_dir = context["features_dir"]
    bound: list[dict[str, Any]] = []
    for stem in stems:
        manifest_path = features_dir / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"no Stage-1 manifest for {stem}; cannot bind raw input")
        raw_path = resolve_raw_source(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            context["raw_dir"],
            stem=stem,
        )
        bound.append({"stem": stem, "raw_source": raw_path.name, "verified": True})
    return bound


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    """Every gate the economic run depends on, and no economic outcome.

    This walks the same artefacts, the same models and the same raw sources the
    run walks, and stops immediately before the first fill is priced. It calls
    no execution function and computes no return, so a savings-bearing field
    cannot exist to be filtered -- and the filter runs anyway.
    """
    state = _verify_population()
    candidates = state["candidates"]
    strong = state["strong"]

    context = _prepare(args)
    built = _build_fits(context)
    runtime, reconciliation = _recomputed_population(context, built, state)
    strong = [c for c in runtime if c.is_strong_consensus]

    stems = sorted({f"{c.symbol}_{c.session_date}" for c in strong})
    bound = _bind_raw_sources(context, stems)

    counts = state["counts"]
    payload = {
        "stage36_plan_version": STAGE36_PLAN_VERSION,
        "stage36_executor_version": STAGE36_EXECUTOR_VERSION,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "design_sha256": EXPECTED_DESIGN_SHA256,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "diagnostic_only": True,
        "contains_economic_outcome": False,
        "why_no_outcome": (
            "this command exercises the gates the run depends on and stops "
            "before a single fill is priced"
        ),
        "frozen_cells": list(FROZEN_CELLS),
        "artifacts": state["artifacts"],
        "measured_events": counts["measured_events"],
        "consensus_counts": _count_records(
            {k: v for k, v in counts.items() if k != "measured_events"}
        ),
        "predictions_checked": state["causal"]["predictions_checked"],
        "all_predictions_within_t0_to_td": state["causal"]["all_within_t0_to_td"],
        "consensus_labels_rederived": state["consistent"]["candidates_rederived"],
        "consensus_labels_consistent": state["consistent"]["labels_consistent"],
        "timing_semantics": state["timing"],
        "sessions_in_population": len({c.session_date for c in candidates}),
        "symbols_in_population": len({c.symbol for c in candidates}),
        "candidate_symbol_days": len(stems),
        "raw_sources_verified": len(bound),
        "raw_sources": bound,
        "batch_completeness": context["batch_completeness"],
        "stage2_reproduction": built["reproduction"],
        "consensus_source": reconciliation["consensus_source"],
        "runtime_recomputation": reconciliation,
        "strong_consensus_recomputed": len(strong),
        "per_session_date_training": context["chronology"],
        "no_date_trains_on_itself": True,
    }
    clean = _strip_outcomes(payload)
    _write(Path(args.output_dir) / "stage36_diagnostic.json", clean)
    return {k: v for k, v in clean.items() if k not in ("raw_sources",)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The single primary economic specification, over the complete population.

    Gated. There is no subset flag: the design freezes one specification across
    the whole frozen candidate set, and evaluating part of it would be a
    different specification wearing the same name.
    """
    from app.services.mbo_book_validator import MboBook, iter_dbn_events
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_executor import BookReplay, resolve_raw_source
    from app.services.mbo_stage35_executor import CoverageTracker
    from app.services.mbo_stage36_executor import (
        FAIL_NO_CONSENSUS,
        Stage36Accumulator,
        execute_candidate,
        write_report,
    )

    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-3.6 economic run is not authorized. This exposes the one "
            "primary specification and advances the ledger to 531 whether it "
            "passes or fails; re-run with --i-have-reviewed-the-design once that "
            "review has happened."
        )

    state = _verify_population()
    context = _prepare(args)
    built = _build_fits(context)
    # The models decide. The census only gets to confirm that they still say
    # what they were frozen saying; if they do not, this refuses before a single
    # fill is priced.
    runtime, reconciliation = _recomputed_population(context, built, state)

    price_scale = float(FIXED_PRICE_SCALE)
    accumulator = Stage36Accumulator(price_scale=price_scale)

    by_stem: dict[str, list[Any]] = {}
    for candidate in runtime:
        if not candidate.is_strong_consensus:
            accumulator.record_failure(FAIL_NO_CONSENSUS)
            continue
        by_stem.setdefault(f"{candidate.symbol}_{candidate.session_date}", []).append(
            candidate
        )

    features_dir = context["features_dir"]
    for index, stem in enumerate(sorted(by_stem), start=1):
        manifest_path = features_dir / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"no Stage-1 manifest for {stem}; cannot bind raw input")
        raw_path = resolve_raw_source(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            context["raw_dir"],
            stem=stem,
        )
        print(f"[{index}/{len(by_stem)}] {stem}", flush=True)

        group = by_stem[stem]
        instants = sorted(
            {c.entry_arrival_ns for c in group} | {c.exit_arrival_ns for c in group}
        )
        coverage = CoverageTracker()
        replay = BookReplay(MboBook)
        books = replay.run(coverage.wrap(iter_dbn_events(str(raw_path))), instants)

        def book_at(ts: int, _books=books):
            return _books.get(ts)

        for candidate in group:
            trade, reason = execute_candidate(
                candidate,
                book_at=book_at,
                price_scale=price_scale,
                timing_certified=replay.timing_certified,
                within_coverage=coverage.covers,
            )
            if trade is None:
                accumulator.record_failure(reason or "unknown")
            else:
                accumulator.record_trade(trade)
        del books, replay

    report = accumulator.summary()
    report["design_sha256"] = state["design"]["sha256"]
    report["manifest_sha256"] = state["artifacts"]["manifest"]["sha256"]
    report["frozen_counts"] = state["counts"]
    report["stage2_reproduction"] = built["reproduction"]
    report["consensus_source"] = reconciliation["consensus_source"]
    report["runtime_recomputation"] = reconciliation
    report["batch_completeness"] = context["batch_completeness"]
    report["evidence_class"] = "exploratory mechanism development"
    report["confirmatory"] = False
    report["prior_effective_trials"] = 530
    report["effective_trials_after_outcome"] = 531
    write_report(report, Path(args.output_dir) / "stage36_results.json")
    return {k: v for k, v in report.items() if k != "per_session_net_bps"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-stage36",
        description=(
            "Stage 3.6: news-triggered L3 consensus economic experiment "
            "(exploratory; authorizes no paper or live trading)."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_cmd = subparsers.add_parser("plan", help="Emit the frozen Stage-3.6 plan.")
    plan_cmd.set_defaults(handler=plan)

    verify_cmd = subparsers.add_parser(
        "verify",
        help="Artefact and population verification only; needs no market data.",
    )
    verify_cmd.set_defaults(handler=verify)

    def add_inputs(cmd):
        cmd.add_argument("--stage2-results", required=True)
        cmd.add_argument("--grams-dir", required=True)
        cmd.add_argument("--features-dir", required=True)
        cmd.add_argument("--labels-dir", required=True)
        cmd.add_argument("--raw-dir", required=True)

    diagnose_cmd = subparsers.add_parser(
        "diagnose", help="Every gate the run depends on; exposes no economic outcome."
    )
    add_inputs(diagnose_cmd)
    diagnose_cmd.set_defaults(handler=diagnose)

    run_cmd = subparsers.add_parser("run", help="The economic experiment (gated).")
    add_inputs(run_cmd)
    # No --limit. The frozen specification is one test over the complete
    # candidate population; a subset of it is a different specification.
    run_cmd.add_argument("--i-have-reviewed-the-design", action="store_true")
    run_cmd.set_defaults(handler=run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(
        args.handler,
        args,
        banner=f"{STAGE36_EXECUTOR_VERSION} :: {args.command} :: "
        f"{STAGE36_PLAN_VERSION} {PLAN_DESIGN_HASH[:12]}",
    )


if __name__ == "__main__":
    main()
