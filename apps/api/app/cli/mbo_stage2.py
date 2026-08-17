"""Stage 2B -- CLI: execute the frozen prediction plan v3.

Two commands, deliberately separable so the expensive pass happens once:

    python -m app.cli.mbo_stage2 grams --features-dir <dir> --labels-dir <dir>
    python -m app.cli.mbo_stage2 run --grams-dir <dir>

``grams`` streams one symbol-day cadence file at a time and reduces it to
per-(cell, session_date) sufficient statistics -- X'X, X'y, y'y, n, sum(y).
That is the only pass over the row-level data, and it writes tens of megabytes
rather than another multi-GB dataset. ``run`` consumes those and applies the
frozen statistics.

The reduction is lossless for this plan because Stage-1 scaling is prior-only
within a symbol-day, so the design matrix does not depend on how dates are
later split.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from app.cli._refusal import run_command
from app.services.mbo_feature_engine import (
    FEATURE_SEMANTICS_HASH,
    FEATURE_VOCABULARY,
)
from app.services.mbo_label_engine import (
    HORIZONS_BY_NAME,
    LABEL_DEFINITION_HASH,
    LABEL_LOGIC_HASH,
    LABEL_OK,
    SUPERSEDED_LABEL_DEFINITION_HASHES,
)
from app.services.mbo_stage2_executor import (
    DESIGN_WIDTH,
    MIN_PRIOR_OBSERVATIONS,
    PRICE_ONLY_WIDTH,
    STAGE2_EXECUTOR_VERSION,
    Gram,
    assert_frozen_plan,
    run_stage2,
    write_summary,
)
from app.services.mbo_stage2_plan import (
    PLAN_DESIGN_HASH,
    PLAN_HASH,
    PRICE_ONLY_LAGS,
    PRIMARY_CELLS,
    SUPERSEDED_PLAN_HASHES,
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "reports" / "tier1_stage2_results"

FEATURE_NAMES = tuple(FEATURE_VOCABULARY)
CADENCES_IN_GRID = tuple(dict.fromkeys(cadence for cadence, _ in PRIMARY_CELLS))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def _assert_frozen_inputs(features_dir: Path, labels_dir: Path) -> dict[str, Any]:
    """Refuse to compute an outcome from artefacts that are not the frozen ones."""
    assert_frozen_plan()
    feature_manifest = json.loads(
        (features_dir / "batch_manifest.json").read_text(encoding="utf-8")
    )
    if feature_manifest.get("definitions", {}).get("feature_semantics_hash") != (
        FEATURE_SEMANTICS_HASH
    ):
        raise ValueError("feature semantics hash is not the frozen Stage-1 v2 artefact")
    label_manifest = json.loads(
        (labels_dir / "label_batch_manifest.json").read_text(encoding="utf-8")
    )
    declared = label_manifest.get("label_definition_hash")
    superseded = {
        entry["label_definition_hash"]: entry
        for entry in SUPERSEDED_LABEL_DEFINITION_HASHES
    }
    reused_under = None
    if declared != LABEL_DEFINITION_HASH:
        if declared not in superseded:
            raise ValueError(
                "label definition hash is neither the current frozen artefact nor "
                "a recorded superseded one"
            )
        # Admissible only because no feature value enters a label. The label
        # logic itself must be untouched, and every spine row is then verified
        # against the regenerated features file by file in `grams` -- assumed
        # nowhere, checked everywhere.
        reused_under = superseded[declared]
        if reused_under.get("label_content_changed") != "false":
            raise ValueError(
                "the superseded label definition changed label content; these "
                "labels must be rebuilt, not reused"
            )
    if label_manifest.get("stage2_plan_hash") not in {
        PLAN_HASH,
        *(entry["plan_hash"] for entry in SUPERSEDED_PLAN_HASHES),
    }:
        raise ValueError("labels were built against a different Stage-2 plan")
    return {
        "feature_semantics_hash": FEATURE_SEMANTICS_HASH,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "label_logic_hash": LABEL_LOGIC_HASH,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "stage2_plan_hash": PLAN_HASH,
        "labels_declared_hash": declared,
        "labels_reused_under_supersession": reused_under,
        "feature_symbol_days": feature_manifest.get("symbol_days"),
        "label_symbol_days_completed": label_manifest.get("symbol_days_completed"),
    }


def _price_only_columns(midpoint: np.ndarray) -> np.ndarray:
    """Lagged own-cadence midpoint log-returns and their signs, prior-only.

    ``lag k`` is the return realized into ``t - k + 1``: lag 1 is the most recent
    completed return, which is known at ``t``. Nothing here reads past ``t``.
    """
    valid = midpoint > 0
    log_mid = np.where(valid, np.log(np.where(valid, midpoint, 1.0)), np.nan)
    returns = np.empty_like(log_mid)
    returns[0] = np.nan
    returns[1:] = log_mid[1:] - log_mid[:-1]

    columns = []
    for lag in PRICE_ONLY_LAGS:
        shifted = np.full_like(returns, np.nan)
        shift = lag - 1
        if shift:
            shifted[shift:] = returns[:-shift]
        else:
            shifted[:] = returns
        columns.append(shifted)
    signs = [np.sign(column) for column in columns]
    return np.column_stack(columns + signs)


def expanding_standardize(
    values: np.ndarray, *, min_priors: int = MIN_PRIOR_OBSERVATIONS
) -> np.ndarray:
    """The frozen plan-v3 scaling: expanding, prior-only, within the symbol-day.

    The mean and standard deviation at row ``t`` are computed from the finite
    observations strictly *before* ``t``. The current observation enters the
    history only after its own standardized value has been determined.

    Three cases, frozen before outcomes:

    - fewer than ``min_priors`` prior finite observations: withheld
    - at least that many, prior SD > 0: the ordinary prior-only z-score
    - at least that many, prior SD == 0: the sensor has been dormant. If the
      current value equals the prior mean it standardizes to exactly ``0.0``;
      if it differs, the row is withheld, because no finite prior scale exists
      to express the difference in.

    That third case matters. Withholding a dormant-but-valid sensor forever
    would let one always-zero column, such as ``modify_count`` on a venue that
    emits no ``M`` records, withhold every row and annihilate the entire design.
    A sensor reading zero is information that nothing happened, not missing
    data.

    Values are shifted by the column's first finite observation before
    accumulating. That is numerically, not statistically, motivated: a z-score
    is invariant to the shift, but ``sum(x^2) - n * mean^2`` loses most of its
    precision when the mean dwarfs the spread -- which is exactly the case for
    the price-level columns. The shift also makes a genuinely constant column
    produce an exact zero variance, so it is withheld rather than dividing tiny
    float dust by tinier float dust.
    """
    finite = np.isfinite(values)
    if not finite.any():
        return np.full(values.shape, np.nan)
    origin = float(values[finite][0])
    centered = np.where(finite, values - origin, 0.0)

    prior_count = np.concatenate(([0.0], np.cumsum(finite.astype(float))[:-1]))
    prior_sum = np.concatenate(([0.0], np.cumsum(centered)[:-1]))
    prior_sumsq = np.concatenate(([0.0], np.cumsum(centered * centered)[:-1]))

    with np.errstate(invalid="ignore", divide="ignore"):
        mean = prior_sum / prior_count
        variance = (prior_sumsq - prior_count * mean * mean) / (prior_count - 1.0)
        deviation = np.sqrt(np.maximum(variance, 0.0))
        standardized = (centered - mean) / deviation

    have_priors = finite & (prior_count >= min_priors)
    varying = deviation > 0.0
    # Dormant: no prior variation, and this row does not break it. The origin
    # shift makes both sides exactly zero for a genuinely constant column, so
    # this equality is exact rather than a tolerance.
    dormant = have_priors & ~varying & (centered == mean)
    return np.where(
        have_priors & varying,
        standardized,
        np.where(dormant, 0.0, np.nan),
    )


def _symbol_day_matrix(table, feature_names) -> tuple[np.ndarray, np.ndarray]:
    """The 70-wide design and the sequence index, before label joining.

    The 59 L3 columns are standardized here rather than in Stage 1: the frozen
    Stage-1 Parquet is never modified, and doing it per symbol-day keeps the
    scaling prior-only and partition-independent, which is what makes the
    per-date Grams additive.
    """
    midpoint = np.asarray(table.column("midpoint").to_numpy(zero_copy_only=False), float)
    price_only = _price_only_columns(midpoint)
    block = np.column_stack(
        [
            expanding_standardize(
                np.asarray(table.column(name).to_numpy(zero_copy_only=False), float)
            )
            for name in feature_names
        ]
    )
    rows = len(midpoint)
    design = np.empty((rows, DESIGN_WIDTH), dtype=float)
    design[:, 0] = 1.0
    design[:, 1:PRICE_ONLY_WIDTH] = price_only
    design[:, PRICE_ONLY_WIDTH:] = block
    sequence = np.asarray(
        table.column("sequence_index").to_numpy(zero_copy_only=False), np.int64
    )
    return design, sequence


def grams(args: argparse.Namespace) -> dict[str, Any]:
    import pyarrow.parquet as pq

    features_dir = Path(args.features_dir)
    labels_dir = Path(args.labels_dir)
    provenance = _assert_frozen_inputs(features_dir, labels_dir)
    output_dir = Path(args.output_dir)

    accumulators: dict[str, dict[str, Gram]] = {
        f"{cadence}|{horizon}": {} for cadence, horizon in PRIMARY_CELLS
    }
    dropped = {
        "rows_seen": 0,
        "rows_used": 0,
        "dropped_label_status_not_ok": 0,
        "dropped_withheld_feature": 0,
        "dropped_insufficient_price_history": 0,
    }
    per_symbol_day: list[dict[str, Any]] = []
    dates: set[str] = set()
    withheld_by_feature: dict[str, int] = {}
    fully_withheld: list[dict[str, str]] = []
    spine_certified = 0

    label_paths = {
        path.name.split(".")[0]: path
        for path in sorted(labels_dir.glob("*.labels.parquet"))
    }
    feature_paths = sorted(features_dir.rglob("*.parquet"))
    if args.limit:
        feature_paths = feature_paths[: args.limit]

    for index, path in enumerate(feature_paths, start=1):
        stem, cadence = path.name.split(".")[0], path.name.split(".")[1]
        if cadence not in CADENCES_IN_GRID:
            continue
        symbol, _, session_date = stem.rpartition("_")
        label_path = label_paths.get(stem)
        if label_path is None:
            raise ValueError(f"no labels for {stem}; refusing a partial cell")
        if not args.quiet:
            print(f"[{index}/{len(feature_paths)}] {stem} {cadence}", flush=True)

        table = pq.read_table(
            path, columns=["sequence_index", "ts_event", *FEATURE_NAMES]
        )
        design, sequence = _symbol_day_matrix(table, FEATURE_NAMES)
        spine_ts = np.asarray(
            table.column("ts_event").to_numpy(zero_copy_only=False), np.int64
        )
        spine_midpoint = np.asarray(
            table.column("midpoint").to_numpy(zero_copy_only=False), float
        )
        del table

        horizons = [h for c, h in PRIMARY_CELLS if c == cadence]
        prefixes = {h: HORIZONS_BY_NAME[h].prefix for h in horizons}
        label_columns = [
            "cadence",
            "sequence_index",
            "source_ts_event",
            "source_midpoint",
        ]
        for prefix in prefixes.values():
            label_columns += [f"{prefix}_status", f"{prefix}_return_bps"]
        labels = pq.read_table(label_path, columns=label_columns)
        mask = np.asarray(
            labels.column("cadence").to_numpy(zero_copy_only=False)
        ) == cadence
        label_sequence = np.asarray(
            labels.column("sequence_index").to_numpy(zero_copy_only=False), np.int64
        )[mask]
        if not np.array_equal(label_sequence, sequence):
            raise ValueError(
                f"label rows for {stem} {cadence} do not align with the feature "
                "snapshots one-for-one; refusing to join on assumption"
            )
        # Spine certification. Labels carry the snapshot they were resolved
        # against; if the regenerated features reproduce that spine exactly then
        # the labels are still the labels of these rows, whatever changed in the
        # feature values. This is what makes reuse across the v2 -> v3 semantics
        # correction a verified fact rather than an argument.
        label_ts = np.asarray(
            labels.column("source_ts_event").to_numpy(zero_copy_only=False), np.int64
        )[mask]
        label_midpoint = np.asarray(
            labels.column("source_midpoint").to_numpy(zero_copy_only=False), float
        )[mask]
        if not np.array_equal(label_ts, spine_ts):
            raise ValueError(
                f"spine mismatch for {stem} {cadence}: label source_ts_event does "
                "not reproduce the feature snapshot timestamps, so these labels "
                "belong to a different extraction and must be rebuilt"
            )
        if not np.array_equal(
            np.nan_to_num(label_midpoint, nan=np.inf),
            np.nan_to_num(spine_midpoint, nan=np.inf),
        ):
            raise ValueError(
                f"spine mismatch for {stem} {cadence}: label source_midpoint does "
                "not reproduce the feature snapshot midpoints, so these labels "
                "must be rebuilt"
            )
        spine_certified += 1

        finite_design = np.isfinite(design).all(axis=1)
        # A feature with no prior variation on this symbol-day is withheld on
        # every row, which removes the whole symbol-day from every cell. That is
        # the declared no-imputation rule doing its job, but it is silent, so
        # count it per feature rather than letting cells quietly shrink.
        block_finite = np.isfinite(design[:, PRICE_ONLY_WIDTH:])
        for position, name in enumerate(FEATURE_NAMES):
            missing = int((~block_finite[:, position]).sum())
            if missing:
                withheld_by_feature[name] = withheld_by_feature.get(name, 0) + missing
            if missing == len(design):
                fully_withheld.append(
                    {"symbol_day": stem, "cadence": cadence, "feature": name}
                )
        history = np.zeros(len(design), dtype=bool)
        history[max(PRICE_ONLY_LAGS) :] = True
        usable_row = finite_design & history

        for horizon in horizons:
            prefix = prefixes[horizon]
            status = np.asarray(
                labels.column(f"{prefix}_status").to_numpy(zero_copy_only=False)
            )[mask]
            target = np.asarray(
                labels.column(f"{prefix}_return_bps").to_numpy(zero_copy_only=False),
                float,
            )[mask]
            ok = (status == LABEL_OK) & np.isfinite(target)
            keep = ok & usable_row
            dropped["rows_seen"] += len(keep)
            dropped["rows_used"] += int(keep.sum())
            dropped["dropped_label_status_not_ok"] += int((~ok).sum())
            dropped["dropped_withheld_feature"] += int((ok & ~finite_design).sum())
            dropped["dropped_insufficient_price_history"] += int(
                (ok & finite_design & ~history).sum()
            )
            if not keep.any():
                continue
            cell = f"{cadence}|{horizon}"
            gram = accumulators[cell].setdefault(session_date, Gram.zeros(DESIGN_WIDTH))
            gram.add_rows(design[keep], target[keep])

        dates.add(session_date)
        per_symbol_day.append(
            {"symbol": symbol, "session_date": session_date, "cadence": cadence}
        )
        del design, labels

    ordered_dates = sorted(dates)
    payload: dict[str, np.ndarray] = {}
    for cell, by_date in accumulators.items():
        for session_date, gram in by_date.items():
            key = f"{cell}||{session_date}"
            payload[f"{key}||xtx"] = gram.xtx
            payload[f"{key}||xty"] = gram.xty
            payload[f"{key}||scalars"] = np.array(
                [gram.yty, float(gram.n), gram.ysum], dtype=float
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    gram_path = output_dir / "stage2_grams.npz"
    np.savez_compressed(gram_path, **payload)

    manifest = {
        "stage2_executor_version": STAGE2_EXECUTOR_VERSION,
        "provenance": provenance,
        "design_width": DESIGN_WIDTH,
        "price_only_width": PRICE_ONLY_WIDTH,
        "cells": sorted(accumulators),
        "session_dates": ordered_dates,
        "session_date_count": len(ordered_dates),
        "symbol_day_cadence_files": len(per_symbol_day),
        "row_accounting": dropped,
        "stage2_scaling": {
            "rule": "expanding, prior-only, within symbol-day, per (symbol, cadence, feature)",
            "applied_to": "the 59 L3 columns",
            "min_prior_observations": MIN_PRIOR_OBSERVATIONS,
            "imputation": "none; withheld rows are dropped",
            "stage1_parquet_modified": False,
        },
        "spine_certified_files": spine_certified,
        "label_reuse": {
            "labels_rebuilt": provenance["labels_declared_hash"] == LABEL_DEFINITION_HASH,
            "spine_verified_every_file": spine_certified
            == len([p for p in feature_paths if p.name.split(".")[1] in CADENCES_IN_GRID]),
            "basis": (
                "no feature value enters a label; labels are resolved from the raw "
                "certified stream against the snapshot spine, and every spine row "
                "was compared against the regenerated features"
            ),
        },
        "withheld_by_feature": dict(sorted(withheld_by_feature.items())),
        "features_fully_withheld_on_a_symbol_day": fully_withheld,
        "grams_bytes": gram_path.stat().st_size,
        "contains_predictive_result": False,
    }
    _write(output_dir / "stage2_grams_manifest.json", manifest)
    return manifest


def _load_grams(grams_dir: Path) -> tuple[dict[str, dict[str, Gram]], list[str]]:
    archive = np.load(grams_dir / "stage2_grams.npz")
    cells: dict[str, dict[str, Gram]] = {}
    dates: set[str] = set()
    for key in archive.files:
        if not key.endswith("||xtx"):
            continue
        cell, session_date, _ = key.split("||")
        base = f"{cell}||{session_date}"
        yty, n, ysum = archive[f"{base}||scalars"]
        cells.setdefault(cell, {})[session_date] = Gram(
            archive[f"{base}||xtx"], archive[f"{base}||xty"], float(yty), int(n), float(ysum)
        )
        dates.add(session_date)
    return cells, sorted(dates)


def run(args: argparse.Namespace) -> dict[str, Any]:
    assert_frozen_plan()
    grams_dir = Path(args.grams_dir)
    cell_blocks, dates = _load_grams(grams_dir)
    result = run_stage2(cell_blocks, dates=dates)
    output_dir = Path(args.output_dir)
    write_summary(result, output_dir / "stage2_results.json")
    return {k: v for k, v in result.items() if k != "cells"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-mbo-stage2",
        description="Stage 2B: execute the frozen Stage-2 prediction plan v3.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    grams_cmd = subparsers.add_parser(
        "grams", help="Reduce features and labels to per-(cell, date) sufficient statistics."
    )
    grams_cmd.add_argument("--features-dir", required=True)
    grams_cmd.add_argument("--labels-dir", required=True)
    grams_cmd.add_argument("--limit", type=int, default=0)
    grams_cmd.add_argument("--quiet", action="store_true")
    grams_cmd.set_defaults(handler=grams)

    run_cmd = subparsers.add_parser(
        "run", help="Apply the frozen statistics to the sufficient statistics."
    )
    run_cmd.add_argument("--grams-dir", required=True)
    run_cmd.set_defaults(handler=run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(
        args.handler,
        args,
        banner=f"{STAGE2_EXECUTOR_VERSION} :: {args.command} :: plan {PLAN_HASH[:12]}",
    )


if __name__ == "__main__":
    main()
