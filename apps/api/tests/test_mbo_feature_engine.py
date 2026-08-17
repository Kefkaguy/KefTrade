"""Stage 1 feature semantics, storage and provenance.

Leakage lives in `test_mbo_feature_leakage.py`. This file pins what each
feature *means* -- above all the two places a sign or a denominator can be
silently wrong.
"""

from __future__ import annotations

import json

import pytest

from app.services.mbo_book_validator import (
    F_BAD_TS_RECV,
    F_LAST,
    FIXED_PRICE_SCALE,
    MboEvent,
)
from app.services.mbo_feature_engine import (
    CADENCES,
    FEATURE_VOCABULARY,
    FEATURE_VOCABULARY_HASH,
    SNAPSHOT_COLUMNS,
    WINDOWED_FEATURES,
    Cadence,
    OrderBookFeatureEngine,
    feature_definitions,
)
from app.services.mbo_feature_store import (
    batch_manifest,
    estimate_storage,
    sha256_file,
    snapshot_schema,
    write_session_features,
)

PX = FIXED_PRICE_SCALE
ONE_EVENT = (Cadence("1ev", "events", 1),)


def opening(ts: int = 0) -> MboEvent:
    return MboEvent(
        ts_event=ts,
        action="R",
        side="N",
        price=0,
        size=0,
        order_id=0,
        flags=F_BAD_TS_RECV,
        sequence=0,
    )


class Clock:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, action, side="B", price=0, size=0, order_id=0, *, flags=F_LAST):
        self.n += 1
        return MboEvent(
            ts_event=self.n * 1_000_000,
            action=action,
            side=side,
            price=price,
            size=size,
            order_id=order_id,
            flags=flags,
            sequence=self.n,
        )


def snapshots(events, *, cadences=ONE_EVENT):
    engine = OrderBookFeatureEngine(
        symbol="TEST", session_date="2025-06-26", cadences=cadences
    )
    return list(engine.process(events))


# ---------------------------------------------------------------------------
# Book state
# ---------------------------------------------------------------------------


def test_book_state_reports_touch_depth_and_counts():
    ev = Clock()
    rows = snapshots(
        [
            opening(),
            ev("A", "B", 34 * PX, 500, 1),
            ev("A", "B", 34 * PX, 300, 2),
            ev("A", "B", 33 * PX, 900, 3),
            ev("A", "A", 34 * PX + 10**7, 400, 4),
        ]
    )
    last = rows[-1]
    assert last["best_bid_price"] == 34 * PX
    assert last["best_ask_price"] == 34 * PX + 10**7
    assert last["spread"] == 10**7
    assert last["midpoint"] == pytest.approx(34 * PX + 5 * 10**6)
    assert last["bid_size_l1"] == 800
    assert last["bid_order_count_l1"] == 2
    assert last["ask_size_l1"] == 400
    assert last["bid_depth_5"] == 1700  # both bid levels
    assert last["bid_levels"] == 2
    assert last["resting_orders"] == 4


def test_spread_bps_is_relative_to_the_midpoint():
    ev = Clock()
    rows = snapshots(
        [opening(), ev("A", "B", 100 * PX, 10, 1), ev("A", "A", 101 * PX, 10, 2)]
    )
    assert rows[-1]["spread_bps"] == pytest.approx(1 * PX / (100.5 * PX) * 10_000)


# ---------------------------------------------------------------------------
# Pressure
# ---------------------------------------------------------------------------


def test_queue_imbalance_and_microprice():
    ev = Clock()
    rows = snapshots(
        [opening(), ev("A", "B", 100 * PX, 300, 1), ev("A", "A", 101 * PX, 100, 2)]
    )
    last = rows[-1]
    assert last["queue_imbalance"] == pytest.approx(0.75)
    assert last["normalized_queue_imbalance"] == pytest.approx(0.5)
    # Microprice leans toward the side with less size waiting.
    assert last["microprice"] == pytest.approx(
        (100 * PX * 100 + 101 * PX * 300) / 400
    )
    assert last["microprice"] > last["midpoint"]
    assert last["microprice_minus_mid"] > 0


def test_microprice_equals_mid_when_the_touch_is_balanced():
    ev = Clock()
    rows = snapshots(
        [opening(), ev("A", "B", 100 * PX, 200, 1), ev("A", "A", 101 * PX, 200, 2)]
    )
    assert rows[-1]["microprice_minus_mid"] == pytest.approx(0.0)
    assert rows[-1]["normalized_queue_imbalance"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The polarity trap
# ---------------------------------------------------------------------------


def test_trade_side_b_is_a_buy_aggressor():
    """Databento: on a Trade, side=B means the aggressor was a buyer."""
    ev = Clock()
    rows = snapshots([opening(), ev("T", "B", 100 * PX, 250, 0)])
    last = rows[-1]
    assert last["buy_aggressor_volume"] == 250
    assert last["sell_aggressor_volume"] == 0
    assert last["signed_trade_volume"] == 250
    assert last["aggressor_imbalance"] == pytest.approx(1.0)


def test_fill_side_b_is_a_sell_aggressor():
    """On a Fill, side=B means a resting BUY was filled -- a seller hit it.

    Signing fills the same way as trades inverts every aggressor feature. This
    is the single most consequential sign in the vocabulary.
    """
    ev = Clock()
    rows = snapshots(
        [opening(), ev("A", "B", 100 * PX, 500, 1), ev("F", "B", 100 * PX, 200, 1)]
    )
    last = rows[-1]
    assert last["sell_aggressor_volume"] == 200
    assert last["buy_aggressor_volume"] == 0
    assert last["signed_trade_volume"] == -200


def test_fill_and_trade_of_the_same_side_letter_have_opposite_signs():
    ev = Clock()
    trade = snapshots([opening(), ev("T", "A", 100 * PX, 100, 0)])[-1]
    ev2 = Clock()
    fill = snapshots(
        [opening(), ev2("A", "A", 100 * PX, 500, 1), ev2("F", "A", 100 * PX, 100, 1)]
    )[-1]
    assert trade["signed_trade_volume"] == -100  # sell aggressor
    assert fill["signed_trade_volume"] == 100  # resting sell filled -> buyer hit it


def test_side_none_trades_are_counted_but_never_signed():
    """Auctions, non-displayed, implied and off-exchange prints.

    They belong in volume and in the unclassified share, never in the sign --
    and the imbalance denominator is classified volume, so unsignable prints
    cannot drag it toward zero.
    """
    ev = Clock()
    rows = snapshots(
        [
            opening(),
            ev("T", "B", 100 * PX, 100, 0),
            ev("T", "N", 100 * PX, 400, 0),
        ],
        cadences=(Cadence("all", "events", 2),),
    )
    last = rows[-1]
    assert last["trade_volume"] == 500
    assert last["unclassified_trade_volume"] == 400
    assert last["unclassified_trade_share"] == pytest.approx(0.8)
    assert last["signed_trade_volume"] == 100
    # Over classified volume (100), not total (500).
    assert last["aggressor_imbalance"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Lifecycle and absorption
# ---------------------------------------------------------------------------


def test_lifecycle_counts_and_ratios_keep_their_primitives():
    ev = Clock()
    rows = snapshots(
        [
            opening(),
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "B", 100 * PX, 500, 2),
            ev("C", "B", 100 * PX, 200, 1),
        ],
        cadences=(Cadence("all", "events", 3),),
    )
    last = rows[-1]
    assert last["add_count"] == 2
    assert last["add_volume"] == 1000
    assert last["cancel_count"] == 1
    assert last["cancel_volume"] == 200
    assert last["cancel_add_ratio"] == pytest.approx(0.5)
    assert last["cancel_volume_ratio"] == pytest.approx(0.2)


def test_ratio_is_none_rather_than_zero_when_the_denominator_is_empty():
    ev = Clock()
    rows = snapshots([opening(), ev("C", "B", 100 * PX, 10, 999)])
    assert rows[-1]["cancel_add_ratio"] is None
    assert rows[-1]["absorption_ratio"] is None


def test_queue_depletion_is_recorded_when_the_touch_empties():
    ev = Clock()
    rows = snapshots(
        [
            opening(),
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "A", 101 * PX, 500, 2),
            ev("C", "B", 100 * PX, 500, 1),
        ],
        cadences=(Cadence("all", "events", 3),),
    )
    assert rows[-1]["queue_depletion_events"] >= 1


def test_executions_without_price_movement_are_absorption():
    ev = Clock()
    rows = snapshots(
        [
            opening(),
            ev("A", "B", 100 * PX, 900, 1),
            ev("A", "A", 101 * PX, 900, 2),
            ev("F", "B", 100 * PX, 100, 1),
            ev("F", "B", 100 * PX, 100, 1),
        ],
        cadences=(Cadence("all", "events", 4),),
    )
    last = rows[-1]
    assert last["execution_count"] == 2
    assert last["executions_without_price_move"] == 2
    assert last["absorption_ratio"] == pytest.approx(1.0)


def test_refill_after_execution_is_attributed_to_the_touch():
    ev = Clock()
    rows = snapshots(
        [
            opening(),
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "A", 101 * PX, 500, 2),
            ev("F", "B", 100 * PX, 100, 1),
            ev("A", "B", 100 * PX, 400, 3),
        ],
        cadences=(Cadence("all", "events", 4),),
    )
    assert rows[-1]["refill_after_execution_volume"] == 400
    assert rows[-1]["touch_replenishment_volume"] >= 400


# ---------------------------------------------------------------------------
# Cadences and windows
# ---------------------------------------------------------------------------


def test_event_cadence_emits_every_n_flast_events():
    ev = Clock()
    events = [opening()] + [ev("A", "B", (100 - i) * PX, 10, i + 1) for i in range(20)]
    rows = snapshots(events, cadences=(Cadence("5ev", "events", 5),))
    assert len(rows) == 4
    assert [row["window_flast_events"] for row in rows] == [5, 5, 5, 5]
    assert [row["sequence_index"] for row in rows] == [0, 1, 2, 3]


def test_windows_do_not_overlap_and_reset_on_emission():
    ev = Clock()
    events = [opening()] + [ev("A", "B", (100 - i) * PX, 10, i + 1) for i in range(10)]
    rows = snapshots(events, cadences=(Cadence("5ev", "events", 5),))
    assert rows[0]["add_count"] == 5
    assert rows[1]["add_count"] == 5, "the second window must not re-count the first"


def test_time_cadence_anchors_to_the_first_event_not_a_fictional_clock():
    events = [opening(ts=1_234_567_890)]
    for i in range(1, 11):
        events.append(
            MboEvent(
                ts_event=1_234_567_890 + i * 400_000_000,
                action="A",
                side="B",
                price=(100 - i) * PX,
                size=10,
                order_id=i,
                flags=F_LAST,
                sequence=i,
            )
        )
    rows = snapshots(events, cadences=(Cadence("1s", "time", 1_000_000_000),))
    assert rows, "a four-second span must produce 1s snapshots"
    assert all(row["ts_event"] >= 1_234_567_890 for row in rows)


def test_all_four_declared_cadences_are_produced():
    events = [opening()]
    for i in range(1, 401):
        events.append(
            MboEvent(
                ts_event=i * 20_000_000,
                action="A",
                side="B" if i % 2 else "A",
                price=(100 * PX - i * 10**6) if i % 2 else (101 * PX + i * 10**6),
                size=10,
                order_id=i,
                flags=F_LAST,
                sequence=i,
            )
        )
    rows = snapshots(events, cadences=CADENCES)
    assert {row["cadence"] for row in rows} == {"1s", "5s", "50ev", "200ev"}


# ---------------------------------------------------------------------------
# Vocabulary and provenance
# ---------------------------------------------------------------------------


def test_every_snapshot_carries_exactly_the_declared_columns():
    ev = Clock()
    rows = snapshots([opening(), ev("A", "B", 100 * PX, 10, 1)])
    assert set(rows[0]) == set(SNAPSHOT_COLUMNS)


def test_vocabulary_hash_is_stable_and_covers_every_feature():
    assert len(FEATURE_VOCABULARY) == len(set(FEATURE_VOCABULARY))
    definitions = feature_definitions()
    assert definitions["feature_vocabulary_hash"] == FEATURE_VOCABULARY_HASH
    grouped = [name for group in definitions["groups"].values() for name in group]
    assert sorted(grouped) == sorted(FEATURE_VOCABULARY)


def test_windowed_features_are_declared_so_they_are_not_read_as_instantaneous():
    assert "add_count" in WINDOWED_FEATURES
    assert "order_flow_imbalance" in WINDOWED_FEATURES
    assert "best_bid_price" not in WINDOWED_FEATURES
    assert "queue_imbalance" not in WINDOWED_FEATURES


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_parquet_round_trip_preserves_the_snapshot(tmp_path):
    import pyarrow.parquet as pq

    ev = Clock()
    events = [opening()] + [ev("A", "B", (100 - i) * PX, 10, i + 1) for i in range(20)]
    output = tmp_path / "features"
    manifest = write_session_features(
        events,
        symbol="TEST",
        session_date="2025-06-26",
        output_dir=output,
        cadences=(Cadence("5ev", "events", 5),),
    )
    assert manifest["total_rows"] == 4
    written = output / "5ev" / "TEST_2025-06-26.5ev.parquet"
    assert written.is_file()
    table = pq.read_table(written)
    assert table.num_rows == 4
    assert set(table.column_names) == set(SNAPSHOT_COLUMNS)
    assert table.column("add_count").to_pylist() == [5, 5, 5, 5]


def test_manifest_records_provenance_and_the_vocabulary_hash(tmp_path):
    source = tmp_path / "xnas-itch-20250626.mbo.TEST.0000.dbn.zst"
    source.write_bytes(b"pretend-dbn-bytes")
    ev = Clock()
    events = [opening()] + [ev("A", "B", (100 - i) * PX, 10, i + 1) for i in range(10)]
    output = tmp_path / "features"
    manifest = write_session_features(
        events,
        symbol="TEST",
        session_date="2025-06-26",
        output_dir=output,
        source_path=source,
        cadences=(Cadence("5ev", "events", 5),),
    )
    assert manifest["feature_vocabulary_hash"] == FEATURE_VOCABULARY_HASH
    assert manifest["validator_version"].startswith("tier1_mbo_book_validator")
    assert manifest["source"]["filename"] == source.name
    assert manifest["source"]["sha256"] == sha256_file(source)
    assert manifest["contains_forward_information"] is False
    on_disk = json.loads(
        (output / "manifests" / "TEST_2025-06-26.manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk == manifest


def test_batch_manifest_flags_a_vocabulary_that_moved(tmp_path):
    consistent = [
        {
            "feature_vocabulary_hash": "abc",
            "feature_engine_version": "v1",
            "total_rows": 10,
            "total_bytes": 100,
            "records_consumed": 5,
            "flast_events": 5,
            "cadences": {},
        },
        {
            "feature_vocabulary_hash": "abc",
            "feature_engine_version": "v1",
            "total_rows": 10,
            "total_bytes": 100,
            "records_consumed": 5,
            "flast_events": 5,
            "cadences": {},
        },
    ]
    assert batch_manifest(tmp_path, consistent)["feature_vocabulary_consistent"] is True

    drifted = [consistent[0], {**consistent[1], "feature_vocabulary_hash": "xyz"}]
    summary = batch_manifest(tmp_path, drifted)
    assert summary["feature_vocabulary_consistent"] is False
    assert summary["feature_vocabulary_hashes"] == ["abc", "xyz"]


def test_storage_estimate_extrapolates_from_measurement_not_a_guess():
    estimate = estimate_storage(symbol_days=2, observed_rows=1_000, observed_bytes=40_000)
    assert estimate["measurable"] is True
    assert estimate["bytes_per_row"] == pytest.approx(40.0)
    assert estimate["projected_160_symbol_day_rows"] == 80_000
    assert estimate["projected_160_symbol_day_bytes"] == 3_200_000
    assert estimate_storage(symbol_days=0, observed_rows=0, observed_bytes=0)["measurable"] is False


# ---------------------------------------------------------------------------
# Integration: the real CMCSA file (opt-in)
# ---------------------------------------------------------------------------

CMCSA_FILE = "xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst"


def _find_cmcsa_file():
    import os
    from pathlib import Path

    override = os.environ.get("KEFTRADE_MBO_TEST_FILE")
    if override and Path(override).is_file():
        return Path(override)
    roots = [
        Path(__file__).resolve().parents[3] / "data" / "databento",
        Path(__file__).resolve().parents[3],
        Path("/opt/keftrade/data/databento"),
        Path("/opt/keftrade"),
    ]
    for root in roots:
        candidate = root / CMCSA_FILE
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.skipif(
    _find_cmcsa_file() is None,
    reason=f"{CMCSA_FILE} not present; set KEFTRADE_MBO_TEST_FILE to run",
)
def test_real_cmcsa_session_produces_causal_features_and_a_storage_measurement(tmp_path):
    """Extract a real symbol-day and measure what it actually costs on disk.

    Asserts structure and causality only. No forward return is computed and no
    predictive claim is made -- this checks that the engine survives a real
    session and that the storage estimate rests on measurement.
    """
    import pyarrow.parquet as pq

    from app.services.mbo_book_validator import iter_dbn_events

    path = _find_cmcsa_file()
    output = tmp_path / "features"
    manifest = write_session_features(
        iter_dbn_events(str(path)),
        symbol="CMCSA",
        session_date="2025-06-26",
        output_dir=output,
        source_path=path,
    )

    assert manifest["records_consumed"] > 100_000
    assert manifest["flast_events"] > 10_000
    assert set(manifest["cadences"]) == {c.name for c in CADENCES}
    assert manifest["feature_vocabulary_hash"] == FEATURE_VOCABULARY_HASH
    assert manifest["source"]["sha256"]

    for cadence in CADENCES:
        entry = manifest["cadences"][cadence.name]
        assert entry["rows"] > 0
        table = pq.read_table(output / entry["path"])
        assert set(table.column_names) == set(SNAPSHOT_COLUMNS)

        ts = table.column("ts_event").to_pylist()
        assert ts == sorted(ts), "snapshots must be emitted in time order"

        # Spot-check causality invariants on real data.
        spreads = [v for v in table.column("spread").to_pylist() if v is not None]
        assert all(v >= 0 for v in spreads), "a reconstructed book must not be crossed"
        shares = [
            v for v in table.column("unclassified_trade_share").to_pylist() if v is not None
        ]
        assert all(0.0 <= v <= 1.0 for v in shares)
        qi = [v for v in table.column("queue_imbalance").to_pylist() if v is not None]
        assert all(0.0 <= v <= 1.0 for v in qi)

    estimate = estimate_storage(
        symbol_days=1,
        observed_rows=manifest["total_rows"],
        observed_bytes=manifest["total_bytes"],
    )
    assert estimate["measurable"] is True
    assert estimate["projected_160_symbol_day_bytes"] > 0
    print(json.dumps({"manifest": manifest, "storage_estimate": estimate}, indent=2))


def test_schema_types_match_the_declared_columns():
    schema = snapshot_schema()
    assert [field.name for field in schema] == list(SNAPSHOT_COLUMNS)
    assert schema.field("symbol").type.equals(__import__("pyarrow").string())
    assert schema.field("add_count").type.equals(__import__("pyarrow").int64())
    assert schema.field("queue_imbalance").type.equals(__import__("pyarrow").float64())
