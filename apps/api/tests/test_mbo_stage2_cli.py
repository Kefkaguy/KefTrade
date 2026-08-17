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
    expanding_standardize,
    grams,
)
from app.services.mbo_feature_engine import FEATURE_SEMANTICS_HASH
from app.services.mbo_label_engine import (
    HORIZONS_BY_NAME,
    LABEL_DEFINITION_HASH,
    SUPERSEDED_LABEL_DEFINITION_HASHES,
)
from app.services.mbo_stage2_executor import (
    DESIGN_WIDTH,
    MIN_PRIOR_OBSERVATIONS,
    PRICE_ONLY_WIDTH,
)
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


# ---------------------------------------------------------------------------
# Correction A -- the frozen Stage-2 scaling rule
# ---------------------------------------------------------------------------


def test_standardization_uses_only_strictly_prior_observations():
    values = np.arange(1.0, 61.0)
    z = expanding_standardize(values, min_priors=5)
    # Row 10 must be standardized by the first ten values and nothing else.
    prior = values[:10]
    expected = (values[10] - prior.mean()) / prior.std(ddof=1)
    assert z[10] == pytest.approx(expected)


def test_values_below_the_prior_minimum_are_withheld_not_imputed():
    values = np.arange(1.0, 61.0)
    z = expanding_standardize(values, min_priors=MIN_PRIOR_OBSERVATIONS)
    assert np.isnan(z[:MIN_PRIOR_OBSERVATIONS]).all()
    assert np.isfinite(z[MIN_PRIOR_OBSERVATIONS:]).all()
    assert not (z[:MIN_PRIOR_OBSERVATIONS] == 0).any()  # withheld, never zero


def test_truncation_invariance_appending_rows_cannot_move_an_earlier_row():
    """The whole point of prior-only: a longer session must not rewrite history."""
    rng = np.random.default_rng(3)
    full = rng.standard_normal(200) * 3.0 + 7.0
    short = expanding_standardize(full[:120], min_priors=MIN_PRIOR_OBSERVATIONS)
    long = expanding_standardize(full, min_priors=MIN_PRIOR_OBSERVATIONS)
    np.testing.assert_allclose(short, long[:120], rtol=1e-12, equal_nan=True)


def test_future_perturbation_cannot_move_an_earlier_row():
    rng = np.random.default_rng(4)
    values = rng.standard_normal(200) * 3.0 + 7.0
    base = expanding_standardize(values, min_priors=MIN_PRIOR_OBSERVATIONS)
    tampered = values.copy()
    tampered[120:] += 500.0  # a violent change, strictly in the future
    after = expanding_standardize(tampered, min_priors=MIN_PRIOR_OBSERVATIONS)
    np.testing.assert_allclose(base[:120], after[:120], rtol=1e-12, equal_nan=True)


@pytest.mark.parametrize("factor", [1e-6, 0.5, 3.0, 1e6])
def test_scale_invariance_of_the_standardized_column(factor):
    """A positive rescaling of the raw input must not change the z-scores."""
    rng = np.random.default_rng(5)
    values = rng.standard_normal(150) * 2.0 + 100.0
    base = expanding_standardize(values, min_priors=MIN_PRIOR_OBSERVATIONS)
    scaled = expanding_standardize(values * factor, min_priors=MIN_PRIOR_OBSERVATIONS)
    np.testing.assert_allclose(base, scaled, rtol=1e-9, equal_nan=True)


def test_shift_invariance_survives_a_large_offset():
    """Price-level columns have a mean that dwarfs their spread."""
    rng = np.random.default_rng(6)
    values = rng.standard_normal(150) * 0.01
    base = expanding_standardize(values, min_priors=MIN_PRIOR_OBSERVATIONS)
    shifted = expanding_standardize(values + 100_000.0, min_priors=MIN_PRIOR_OBSERVATIONS)
    np.testing.assert_allclose(base, shifted, rtol=1e-6, equal_nan=True)


def test_a_permanently_constant_column_contributes_zero_not_nothing():
    """A dormant sensor such as modify_count on a venue that emits no M records
    must contribute 0, not withhold every row and annihilate the design."""
    constant = np.full(100, 4.0)
    z = expanding_standardize(constant, min_priors=MIN_PRIOR_OBSERVATIONS)
    assert np.isnan(z[:MIN_PRIOR_OBSERVATIONS]).all()
    assert (z[MIN_PRIOR_OBSERVATIONS:] == 0.0).all()


def test_a_dormant_column_of_zeros_also_contributes_zero():
    z = expanding_standardize(np.zeros(100), min_priors=MIN_PRIOR_OBSERVATIONS)
    assert (z[MIN_PRIOR_OBSERVATIONS:] == 0.0).all()


def test_the_row_that_breaks_a_constant_history_is_withheld():
    """No finite prior scale exists to express the break in, so it is withheld
    rather than dividing by a zero standard deviation."""
    values = np.full(100, 4.0)
    values[60] = 9.0
    z = expanding_standardize(values, min_priors=MIN_PRIOR_OBSERVATIONS)
    assert z[59] == 0.0                       # still dormant
    assert np.isnan(z[60])                    # the break itself: withheld
    assert np.isfinite(z[61])                 # a prior scale now exists
    assert z[61] != 0.0


def test_the_breaking_observation_enters_history_only_after_being_scored():
    """Row 60 must be standardized against the constant history alone."""
    values = np.full(100, 4.0)
    values[60] = 9.0
    z = expanding_standardize(values, min_priors=MIN_PRIOR_OBSERVATIONS)
    prior = values[:61]
    expected = (values[61] - prior.mean()) / prior.std(ddof=1)
    assert z[61] == pytest.approx(expected)


def test_non_finite_inputs_do_not_contaminate_later_statistics():
    values = np.arange(1.0, 81.0)
    holed = values.copy()
    holed[5] = np.nan
    z = expanding_standardize(holed, min_priors=5)
    prior = np.array([v for v in holed[:20] if np.isfinite(v)])
    expected = (holed[20] - prior.mean()) / prior.std(ddof=1)
    assert z[20] == pytest.approx(expected)
    assert np.isnan(z[5])


def test_the_l3_block_reaching_the_design_is_standardized_not_raw(frozen_tree):
    features, _ = frozen_tree
    cadence = PRIMARY_CELLS[0][0]
    path = next((features / cadence).glob("*.parquet"))
    table = pq.read_table(path)
    design, _ = _symbol_day_matrix(table, FEATURE_NAMES)
    midpoint_column = design[:, PRICE_ONLY_WIDTH + FEATURE_NAMES.index("midpoint")]
    raw = np.asarray(table.column("midpoint").to_numpy(zero_copy_only=False), float)
    # Standardized, so it must not still be a price near 100.
    assert np.isnan(midpoint_column[:MIN_PRIOR_OBSERVATIONS]).all()
    tail = midpoint_column[MIN_PRIOR_OBSERVATIONS:]
    assert np.abs(tail[np.isfinite(tail)]).max() < 50.0
    assert not np.allclose(midpoint_column[MIN_PRIOR_OBSERVATIONS:],
                           raw[MIN_PRIOR_OBSERVATIONS:], equal_nan=True)


def _feature_table(seed: int) -> pa.Table:
    rng = np.random.default_rng(seed)
    columns = {
        "sequence_index": pa.array(np.arange(ROWS), pa.int64()),
        "ts_event": pa.array(np.arange(ROWS, dtype=np.int64) * 1_000_000, pa.int64()),
    }
    for name in FEATURE_NAMES:
        if name == "midpoint":
            values = 100 + np.cumsum(rng.standard_normal(ROWS) * 0.01)
        else:
            values = rng.standard_normal(ROWS)
        columns[name] = pa.array(values, pa.float64())
    return pa.table(columns)


def _label_table(cadence: str, seed: int, features: pa.Table) -> pa.Table:
    """Labels carry the spine of the snapshot they were resolved against."""
    rng = np.random.default_rng(seed + 7)
    columns = {
        "cadence": pa.array([cadence] * ROWS, pa.string()),
        "sequence_index": pa.array(np.arange(ROWS), pa.int64()),
        "source_ts_event": features.column("ts_event"),
        "source_midpoint": features.column("midpoint"),
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
            built = {}
            for cadence in cadences:
                seed += 1
                directory = features / cadence
                directory.mkdir(parents=True, exist_ok=True)
                built[cadence] = (_feature_table(seed), seed)
                pq.write_table(
                    built[cadence][0], directory / f"{stem}.{cadence}.parquet"
                )
            labels.mkdir(parents=True, exist_ok=True)
            tables = [
                _label_table(cadence, built[cadence][1], built[cadence][0])
                for cadence in cadences
            ]
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
    # The binding constraint is now the 30 priors the scaling rule requires,
    # which subsumes both the 3 unusable statuses and the 10 lag rows.
    usable_per_symbol = ROWS - MIN_PRIOR_OBSERVATIONS
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
    # The L3 block starts exactly where the price-only block ends. Its columns
    # are standardized, so identity is checked against the standardized column.
    midpoint_index = PRICE_ONLY_WIDTH + FEATURE_NAMES.index("midpoint")
    np.testing.assert_allclose(
        design[:, midpoint_index],
        expanding_standardize(
            np.asarray(table.column("midpoint").to_numpy(zero_copy_only=False), float)
        ),
        equal_nan=True,
    )


def test_labels_from_the_superseded_v2_binding_are_reusable(frozen_tree, tmp_path):
    """The v3 absorption correction changed feature values, not the spine, so
    labels resolved under the v2 binding remain the labels of these rows."""
    features, labels = frozen_tree
    manifest = json.loads((labels / "label_batch_manifest.json").read_text())
    manifest["label_definition_hash"] = SUPERSEDED_LABEL_DEFINITION_HASHES[0][
        "label_definition_hash"
    ]
    (labels / "label_batch_manifest.json").write_text(json.dumps(manifest))
    result = grams(_args(features, labels, tmp_path / "out"))
    assert result["label_reuse"]["labels_rebuilt"] is False
    assert result["label_reuse"]["spine_verified_every_file"] is True
    assert result["spine_certified_files"] > 0


def test_a_spine_mismatch_is_refused_rather_than_reused(frozen_tree, tmp_path):
    """Reuse is admissible only against a spine proved identical. Move one
    timestamp and the labels must be rejected, not silently joined."""
    features, labels = frozen_tree
    path = next(labels.glob("*.labels.parquet"))
    table = pq.read_table(path)
    ts = np.array(table.column("source_ts_event").to_numpy(zero_copy_only=False), np.int64)
    ts[7] += 1
    table = table.set_column(
        table.schema.get_field_index("source_ts_event"),
        "source_ts_event",
        pa.array(ts, pa.int64()),
    )
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="spine mismatch"):
        grams(_args(features, labels, tmp_path / "out"))


def test_a_midpoint_spine_mismatch_is_also_refused(frozen_tree, tmp_path):
    features, labels = frozen_tree
    path = next(labels.glob("*.labels.parquet"))
    table = pq.read_table(path)
    mid = np.array(table.column("source_midpoint").to_numpy(zero_copy_only=False), float)
    mid[11] += 0.01
    table = table.set_column(
        table.schema.get_field_index("source_midpoint"),
        "source_midpoint",
        pa.array(mid, pa.float64()),
    )
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="spine mismatch"):
        grams(_args(features, labels, tmp_path / "out"))


def test_stale_artefacts_are_refused(frozen_tree, tmp_path):
    features, labels = frozen_tree
    (labels / "label_batch_manifest.json").write_text(
        json.dumps({"label_definition_hash": "0" * 64, "stage2_plan_hash": PLAN_HASH}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="label definition hash"):
        grams(_args(features, labels, tmp_path / "out"))
