"""Stage 2B: the reduction from Parquet to sufficient statistics must be exact.

The executor is tested against synthetic Grams elsewhere. What is tested here is
the step that produces those Grams from the frozen Stage-1 and Stage-2A files,
because a wrong join or a wrong lag column would be invisible downstream.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from app.cli.mbo_stage2 import (
    FEATURE_NAMES,
    _load_grams,
    _price_only_columns,
    _symbol_day_matrix,
    build_parser,
    grams,
)
from app.services.mbo_feature_engine import FEATURE_SEMANTICS_HASH
from app.services.mbo_label_engine import HORIZONS_BY_NAME, LABEL_DEFINITION_HASH
from app.services.mbo_stage2_executor import DESIGN_WIDTH, PRICE_ONLY_WIDTH
from app.services.mbo_stage2_plan import PLAN_HASH, PRICE_ONLY_LAGS, PRIMARY_CELLS

DATES = ["2025-06-02", "2025-06-03"]
SYMBOLS = ["AAAA", "BBBB"]
ROWS = 80


def test_price_only_lag_one_is_the_most_recent_completed_return():
    midpoint = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    columns = _price_only_columns(midpoint)
    returns = np.diff(np.log(midpoint))
    # lag 1 at t=5 is the return into t=5.
    assert columns[5, 0] == pytest.approx(returns[4])
    # lag 2 at t=5 is the return into t=4.
    assert columns[5, 1] == pytest.approx(returns[3])
    # lag 10 is unavailable this early and must be nan, not zero.
    assert np.isnan(columns[5, PRICE_ONLY_LAGS.index(10)])
    assert columns.shape[1] == 2 * len(PRICE_ONLY_LAGS)


def test_price_only_columns_never_read_the_future():
    """Perturbing the tail cannot move any earlier row."""
    midpoint = 100 + np.cumsum(np.random.default_rng(0).standard_normal(50) * 0.01)
    base = _price_only_columns(midpoint)
    tampered = midpoint.copy()
    tampered[30:] += 5.0
    after = _price_only_columns(tampered)
    np.testing.assert_allclose(base[:30], after[:30], equal_nan=True)


def _feature_table(seed: int) -> pa.Table:
    rng = np.random.default_rng(seed)
    columns = {"sequence_index": pa.array(np.arange(ROWS), pa.int64())}
    for name in FEATURE_NAMES:
        if name == "midpoint":
            values = 100 + np.cumsum(rng.standard_normal(ROWS) * 0.01)
        else:
            values = rng.standard_normal(ROWS)
        columns[name] = pa.array(values, pa.float64())
    return pa.table(columns)


def _label_table(cadence: str, seed: int) -> pa.Table:
    rng = np.random.default_rng(seed + 7)
    columns = {
        "cadence": pa.array([cadence] * ROWS, pa.string()),
        "sequence_index": pa.array(np.arange(ROWS), pa.int64()),
    }
    for cadence_name, horizon in PRIMARY_CELLS:
        if cadence_name != cadence:
            continue
        prefix = HORIZONS_BY_NAME[horizon].prefix
        status = np.array(["ok"] * ROWS, dtype=object)
        status[:3] = "session_end_before_horizon"  # never usable
        columns[f"{prefix}_status"] = pa.array(list(status), pa.string())
        columns[f"{prefix}_return_bps"] = pa.array(
            rng.standard_normal(ROWS), pa.float64()
        )
    return pa.table(columns)


@pytest.fixture()
def frozen_tree(tmp_path: Path) -> tuple[Path, Path]:
    features = tmp_path / "features"
    labels = tmp_path / "labels"
    cadences = sorted({c for c, _ in PRIMARY_CELLS})
    seed = 0
    for date in DATES:
        for symbol in SYMBOLS:
            stem = f"{symbol}_{date}"
            for cadence in cadences:
                seed += 1
                directory = features / cadence
                directory.mkdir(parents=True, exist_ok=True)
                pq.write_table(_feature_table(seed), directory / f"{stem}.{cadence}.parquet")
            labels.mkdir(parents=True, exist_ok=True)
            tables = [_label_table(cadence, seed) for cadence in cadences]
            pq.write_table(pa.concat_tables(tables, promote_options="default"),
                           labels / f"{stem}.labels.parquet")
    (features / "batch_manifest.json").write_text(
        json.dumps({"definitions": {"feature_semantics_hash": FEATURE_SEMANTICS_HASH},
                    "symbol_days": len(DATES) * len(SYMBOLS)}), encoding="utf-8")
    (labels / "label_batch_manifest.json").write_text(
        json.dumps({"label_definition_hash": LABEL_DEFINITION_HASH,
                    "stage2_plan_hash": PLAN_HASH,
                    "symbol_days_completed": len(DATES) * len(SYMBOLS)}), encoding="utf-8")
    return features, labels


def _args(features: Path, labels: Path, out: Path):
    parser = build_parser()
    return parser.parse_args(
        ["--output-dir", str(out), "grams", "--features-dir", str(features),
         "--labels-dir", str(labels), "--quiet"]
    )


def test_grams_reduce_to_one_block_per_cell_and_date(frozen_tree, tmp_path):
    features, labels = frozen_tree
    out = tmp_path / "out"
    manifest = grams(_args(features, labels, out))
    assert manifest["session_date_count"] == len(DATES)
    assert set(manifest["cells"]) == {f"{c}|{h}" for c, h in PRIMARY_CELLS}
    assert manifest["contains_predictive_result"] is False

    cells, dates = _load_grams(out)
    assert dates == sorted(DATES)
    for cell, by_date in cells.items():
        assert sorted(by_date) == sorted(DATES), cell
        for gram in by_date.values():
            assert gram.xtx.shape == (DESIGN_WIDTH, DESIGN_WIDTH)
            assert gram.n > 0


def test_a_date_block_sums_both_symbols(frozen_tree, tmp_path):
    """Per-date Grams pool the symbols traded that session; the split is by date."""
    features, labels = frozen_tree
    out = tmp_path / "out"
    grams(_args(features, labels, out))
    cells, _ = _load_grams(out)
    gram = cells[f"{PRIMARY_CELLS[0][0]}|{PRIMARY_CELLS[0][1]}"][DATES[0]]
    # 3 unusable statuses and the first max(lag) rows lack price history.
    usable_per_symbol = ROWS - max(PRICE_ONLY_LAGS)
    assert gram.n == len(SYMBOLS) * usable_per_symbol


def test_rows_whose_label_status_is_not_ok_are_excluded(frozen_tree, tmp_path):
    features, labels = frozen_tree
    out = tmp_path / "out"
    manifest = grams(_args(features, labels, out))
    assert manifest["row_accounting"]["dropped_label_status_not_ok"] > 0
    assert manifest["row_accounting"]["rows_used"] < manifest["row_accounting"]["rows_seen"]


def test_a_withheld_feature_drops_the_row_rather_than_imputing_it(frozen_tree, tmp_path):
    """Stage-1 withholds a normalization below 30 priors; it must not become 0."""
    features, labels = frozen_tree
    cadence = PRIMARY_CELLS[0][0]
    path = next((features / cadence).glob("*.parquet"))
    table = pq.read_table(path)
    column = np.array(table.column("spread_bps_z").to_numpy(zero_copy_only=False), float)
    column[40:45] = np.nan
    table = table.set_column(
        table.schema.get_field_index("spread_bps_z"),
        "spread_bps_z",
        pa.array(column, pa.float64()),
    )
    pq.write_table(table, path)

    out = tmp_path / "out"
    manifest = grams(_args(features, labels, out))
    assert manifest["row_accounting"]["dropped_withheld_feature"] > 0


def test_a_misaligned_label_join_is_refused_not_guessed(frozen_tree, tmp_path):
    features, labels = frozen_tree
    path = next(labels.glob("*.labels.parquet"))
    table = pq.read_table(path)
    pq.write_table(table.slice(0, len(table) - 5), path)
    with pytest.raises(ValueError, match="do not align"):
        grams(_args(features, labels, tmp_path / "out"))


def test_the_design_is_intercept_then_price_only_then_the_l3_block():
    table = _feature_table(1)
    design, sequence = _symbol_day_matrix(table, FEATURE_NAMES)
    assert design.shape == (ROWS, DESIGN_WIDTH)
    assert (design[:, 0] == 1.0).all()
    assert len(sequence) == ROWS
    # The L3 block starts exactly where the price-only block ends.
    midpoint_index = PRICE_ONLY_WIDTH + FEATURE_NAMES.index("midpoint")
    np.testing.assert_allclose(
        design[:, midpoint_index],
        np.asarray(table.column("midpoint").to_numpy(zero_copy_only=False), float),
    )


def test_stale_artefacts_are_refused(frozen_tree, tmp_path):
    features, labels = frozen_tree
    (labels / "label_batch_manifest.json").write_text(
        json.dumps({"label_definition_hash": "0" * 64, "stage2_plan_hash": PLAN_HASH}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="label definition hash"):
        grams(_args(features, labels, tmp_path / "out"))
