"""Stage 2A v2: event-time label semantics, wide storage, and label leakage.

No feature-label relationship is computed anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.mbo_book_validator import (
    F_BAD_TS_RECV,
    F_LAST,
    FIXED_PRICE_SCALE,
    MboEvent,
)
from app.services.mbo_label_engine import (
    HORIZON_NAMES,
    HORIZONS,
    LABEL_COLUMNS,
    LABEL_NO_FURTHER_MIDPOINT_CHANGE,
    LABEL_OK,
    LABEL_SESSION_END_BEFORE_HORIZON,
    LABEL_SOURCE_MIDPOINT_UNAVAILABLE,
    NANOS_PER_SECOND,
    REQUIRED_FEATURE_SEMANTICS_HASH,
    SHARED_COLUMNS,
    SUPERSEDED_LABEL_VERSIONS,
    SourceSnapshot,
    label_definitions,
    label_status_summary,
    resolve_symbol_day_labels,
)

S = NANOS_PER_SECOND
PX = FIXED_PRICE_SCALE
MS = 1_000_000


def opening(ts=0):
    return MboEvent(
        ts_event=ts,
        action="R",
        side="N",
        price=0,
        size=0,
        order_id=0,
        flags=F_BAD_TS_RECV,
        sequence=0,
        ts_recv=ts + 1,
    )


def ev(ts, action, side, price, size, order_id, seq, *, recv=None, flags=F_LAST):
    return MboEvent(
        ts_event=ts,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        flags=flags,
        sequence=seq,
        ts_recv=ts + 1_000 if recv is None else recv,
    )


def source(ts, midpoint, *, cadence="1s", index=0, recv=None):
    return SourceSnapshot(
        cadence=cadence,
        sequence_index=index,
        ts_event=ts,
        grid_ts_event=ts,
        midpoint=midpoint,
        ts_recv=ts + 500 if recv is None else recv,
        feature_available_ts_recv=ts + 500 if recv is None else recv,
    )


def resolve(sources, events):
    return resolve_symbol_day_labels(
        symbol="TEST", session_date="2025-06-26", sources=sources, events=events
    )


def two_sided_book(start_ts, *, bid=100 * PX, ask=101 * PX):
    """Open a coherent book so midpoints exist from the start."""
    return [
        opening(start_ts - 2),
        ev(start_ts - 1, "A", "B", bid, 100, 1, 1),
        ev(start_ts, "A", "A", ask, 100, 2, 2),
    ]


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_labels_come_from_the_event_stream_not_the_sampled_cadence():
    definitions = label_definitions()
    assert definitions["derived_from_sampled_cadence"] is False
    assert "certified XNAS MBO stream" in definitions["label_source"]
    assert definitions["storage"].startswith("wide")
    assert definitions["row_multiplier_vs_sources"] == 1
    assert definitions["built_against"]["feature_semantics_hash"] == (
        REQUIRED_FEATURE_SEMANTICS_HASH
    )
    assert definitions["contains_predictive_result"] is False


def test_v1_is_preserved_as_superseded_before_outcome():
    assert len(SUPERSEDED_LABEL_VERSIONS) == 1
    v1 = SUPERSEDED_LABEL_VERSIONS[0]
    assert v1["version"] == "tier1_mbo_label_engine_v1"
    assert v1["commit"].startswith("f3289c9")
    assert v1["superseded_before_outcome"] == "true"
    assert "sampled Stage-1 cadence sequence" in v1["reason"]


# ---------------------------------------------------------------------------
# B. Wide storage
# ---------------------------------------------------------------------------


def test_one_row_per_source_snapshot_not_seven():
    events = two_sided_book(10 * S)
    for i in range(1, 40):
        events.append(ev(10 * S + i * S, "A", "B", (100 + i) * PX, 100, 100 + i, 100 + i))
    sources = [source(10 * S + i * S, 100.5 * PX, index=i) for i in range(5)]
    rows = resolve(sources, events)
    assert len(rows) == len(sources) == 5, "wide storage: one row per snapshot"
    for row in rows:
        assert set(row) == set(LABEL_COLUMNS)
    # Every horizon has its own columns on the same row.
    assert len(LABEL_COLUMNS) == len(SHARED_COLUMNS) + 9 * len(HORIZONS)


# ---------------------------------------------------------------------------
# A. Exact event-time change horizons
# ---------------------------------------------------------------------------


def test_next_change_resolves_at_event_time_not_at_the_next_cadence_boundary():
    """The v1 defect: a 1s source resolved at the next *second* whose midpoint
    differed. The true next change here is one millisecond later."""
    events = two_sided_book(10 * S)
    # Mid moves 1 ms after the source instant.
    events.append(ev(10 * S + MS, "A", "B", 100 * PX + 10**7, 100, 3, 3))
    # And keeps moving, well inside the same second.
    events.append(ev(10 * S + 2 * MS, "A", "B", 100 * PX + 2 * 10**7, 100, 4, 4))
    events.append(ev(20 * S, "A", "A", 105 * PX, 100, 5, 5))

    source_mid = (100 * PX + 101 * PX) / 2
    rows = resolve([source(10 * S, source_mid)], events)
    row = rows[0]
    assert row["next_change_status"] == LABEL_OK
    assert row["next_change_label_ts_event"] == 10 * S + MS
    assert row["next_change_label_ts_event"] - row["source_ts_event"] == MS


def test_next_two_changes_is_the_second_event_time_change():
    events = two_sided_book(10 * S)
    events.append(ev(10 * S + MS, "A", "B", 100 * PX + 10**7, 100, 3, 3))
    events.append(ev(10 * S + 2 * MS, "A", "B", 100 * PX + 2 * 10**7, 100, 4, 4))
    events.append(ev(20 * S, "A", "A", 105 * PX, 100, 5, 5))

    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["next_2_changes_label_ts_event"] == 10 * S + 2 * MS
    assert row["next_2_changes_label_ts_event"] > row["next_change_label_ts_event"]


def test_a_repeated_midpoint_is_not_a_change():
    events = two_sided_book(10 * S)
    # Same midpoint restated several times, then a genuine move.
    for i in range(1, 5):
        events.append(ev(10 * S + i * MS, "A", "B", 100 * PX, 50, 10 + i, 10 + i))
    events.append(ev(10 * S + 9 * MS, "A", "B", 100 * PX + 10**7, 100, 20, 20))
    events.append(ev(20 * S, "A", "A", 105 * PX, 100, 21, 21))

    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["next_change_label_ts_event"] == 10 * S + 9 * MS


def test_a_one_sided_book_is_not_a_midpoint_change():
    events = two_sided_book(10 * S)
    # Cancel the whole ask: the book becomes one-sided, which is not a change.
    events.append(ev(10 * S + MS, "C", "A", 101 * PX, 100, 2, 3))
    # Restore a different ask: now the midpoint has genuinely changed.
    events.append(ev(10 * S + 2 * MS, "A", "A", 102 * PX, 100, 4, 4))
    events.append(ev(20 * S, "A", "B", 100 * PX, 100, 5, 5))

    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["next_change_label_ts_event"] == 10 * S + 2 * MS


def test_a_flat_book_yields_no_change_labels():
    events = two_sided_book(10 * S)
    for i in range(1, 20):
        events.append(ev(10 * S + i * MS, "A", "B", 100 * PX, 10, 100 + i, 100 + i))
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["next_change_status"] == LABEL_NO_FURTHER_MIDPOINT_CHANGE
    assert row["next_2_changes_status"] == LABEL_NO_FURTHER_MIDPOINT_CHANGE
    assert row["next_change_future_midpoint"] is None


# ---------------------------------------------------------------------------
# A. Time horizons: at or after the target, never before
# ---------------------------------------------------------------------------


def test_time_label_is_the_first_state_at_or_after_the_target():
    events = two_sided_book(10 * S)
    for i in range(1, 2_100):
        events.append(ev(10 * S + i * MS, "A", "B", (100 * PX) + (i % 4) * 10**7, 100, 1000 + i, 1000 + i))
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["h1s_target_ts_event"] == 11 * S
    assert row["h1s_label_ts_event"] >= 11 * S
    assert row["h1s_realized_lag_ns"] >= 0
    assert row["h1s_realized_lag_ns"] < MS, "a 1 ms stream should land within a tick"


def test_a_sparse_future_records_the_lag_and_never_shortens_the_horizon():
    events = two_sided_book(10 * S)
    events.append(ev(17 * S, "A", "B", 104 * PX, 100, 3, 3))
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["h1s_target_ts_event"] == 11 * S
    assert row["h1s_label_ts_event"] == 17 * S
    assert row["h1s_realized_lag_ns"] == 6 * S


def test_no_state_at_or_after_the_target_is_missing_not_backfilled():
    events = two_sided_book(10 * S)
    events.append(ev(10 * S + 100 * MS, "A", "B", 100 * PX + 10**7, 100, 3, 3))
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    # A 60s horizon cannot resolve inside a 100 ms session.
    assert row["h60s_status"] == LABEL_SESSION_END_BEFORE_HORIZON
    assert row["h60s_future_midpoint"] is None
    assert row["h60s_return_bps"] is None
    # But the shorter change horizon did resolve.
    assert row["next_change_status"] == LABEL_OK


def test_longer_horizons_run_out_before_shorter_ones():
    events = two_sided_book(10 * S)
    for i in range(1, 12):
        events.append(ev(10 * S + i * S, "A", "B", (100 + i) * PX, 100, 100 + i, 100 + i))
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["h1s_status"] == LABEL_OK
    assert row["h5s_status"] == LABEL_OK
    assert row["h60s_status"] == LABEL_SESSION_END_BEFORE_HORIZON


def test_every_time_horizon_resolves_to_its_own_instant():
    events = two_sided_book(10 * S)
    for i in range(1, 700):
        events.append(
            ev(10 * S + i * 100 * MS, "A", "B", (100 * PX) + (i % 5) * 10**7, 100, 1000 + i, 1000 + i)
        )
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    instants = [row[f"h{name}_label_ts_event"] for name in ("1s", "5s", "10s", "30s", "60s")]
    assert all(value is not None for value in instants)
    assert instants == sorted(instants)
    assert len(set(instants)) == 5, "distinct horizons must not collapse onto one label"


def test_an_incoherent_source_midpoint_blocks_every_horizon():
    events = two_sided_book(10 * S)
    events.append(ev(15 * S, "A", "B", 103 * PX, 100, 3, 3))
    row = resolve([source(10 * S, None)], events)[0]
    for horizon in HORIZONS:
        assert row[f"{horizon.prefix}_status"] == LABEL_SOURCE_MIDPOINT_UNAVAILABLE
        assert row[f"{horizon.prefix}_future_midpoint"] is None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_return_and_change_are_relative_to_the_source_midpoint():
    events = two_sided_book(10 * S)
    # Move the bid up one dollar: mid goes 100.5 -> 101.0
    events.append(ev(11 * S, "A", "B", 101 * PX, 500, 3, 3))
    events.append(ev(12 * S, "A", "B", 101 * PX, 500, 4, 4))
    source_mid = 100.5 * PX
    row = resolve([source(10 * S, source_mid)], events)[0]
    assert row["source_midpoint"] == source_mid
    assert row["h1s_midpoint_change"] == pytest.approx(0.5 * PX)
    assert row["h1s_return_bps"] == pytest.approx(0.5 / 100.5 * 10_000)


def test_label_availability_never_precedes_the_feature_row_or_the_label_record():
    events = two_sided_book(10 * S)
    for i in range(1, 200):
        events.append(
            ev(10 * S + i * 50 * MS, "A", "B", (100 * PX) + (i % 3) * 10**7, 100,
               1000 + i, 1000 + i, recv=10 * S + i * 50 * MS + 7_000)
        )
    source_mid = (100 * PX + 101 * PX) / 2
    row = resolve([source(10 * S, source_mid)], events)[0]
    for horizon in HORIZONS:
        if row[f"{horizon.prefix}_status"] != LABEL_OK:
            continue
        assert (
            row[f"{horizon.prefix}_available_ts_recv"]
            >= row["source_feature_available_ts_recv"]
        )
        assert (
            row[f"{horizon.prefix}_available_ts_recv"]
            >= row[f"{horizon.prefix}_label_ts_recv"]
        )
        assert row[f"{horizon.prefix}_label_ts_event"] > row["source_ts_event"]


def test_labels_never_cross_a_symbol_day():
    """Resolution is confined to the events handed to it, which are one file."""
    monday_events = two_sided_book(10 * S) + [ev(11 * S, "A", "B", 101 * PX, 100, 3, 3)]
    row = resolve([source(10 * S, 100.5 * PX)], monday_events)[0]
    # The 60s horizon finds nothing; the next session is not consulted.
    assert row["h60s_status"] == LABEL_SESSION_END_BEFORE_HORIZON
    assert row["session_date"] == "2025-06-26"


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------


def test_extending_the_stream_cannot_change_an_already_resolved_label():
    base = two_sided_book(10 * S)
    for i in range(1, 400):
        base.append(
            ev(10 * S + i * 20 * MS, "A", "B", (100 * PX) + (i % 4) * 10**7, 100, 1000 + i, 1000 + i)
        )
    extension = [
        ev(10 * S + 8 * S + i * 20 * MS, "A", "B", (500 * PX) + i * 10**7, 100, 9000 + i, 9000 + i)
        for i in range(1, 200)
    ]
    sources = [source(10 * S + i * 500 * MS, 100.5 * PX, index=i) for i in range(6)]

    short = {(r["cadence"], r["sequence_index"]): r for r in resolve(sources, base)}
    long = {(r["cadence"], r["sequence_index"]): r for r in resolve(sources, base + extension)}

    for key, row in short.items():
        for horizon in HORIZONS:
            prefix = horizon.prefix
            if row[f"{prefix}_status"] != LABEL_OK:
                continue  # a status that was pending may legitimately resolve later
            for suffix in ("label_ts_event", "future_midpoint", "return_bps", "realized_lag_ns"):
                assert long[key][f"{prefix}_{suffix}"] == row[f"{prefix}_{suffix}"], (
                    f"{key} {prefix}_{suffix} changed when the stream was extended"
                )


def test_a_label_never_reads_a_state_before_its_target():
    events = two_sided_book(10 * S)
    for i in range(1, 800):
        events.append(
            ev(10 * S + i * 100 * MS, "A", "B", (100 * PX) + (i % 6) * 10**7, 100, 1000 + i, 1000 + i)
        )
    sources = [source(10 * S + i * S, 100.5 * PX, index=i) for i in range(10)]
    for row in resolve(sources, events):
        for horizon in HORIZONS:
            if horizon.kind != "time" or row[f"{horizon.prefix}_status"] != LABEL_OK:
                continue
            assert (
                row[f"{horizon.prefix}_label_ts_event"]
                >= row[f"{horizon.prefix}_target_ts_event"]
            )
            assert row[f"{horizon.prefix}_realized_lag_ns"] >= 0


def test_a_source_is_never_labelled_by_a_state_at_or_before_itself():
    events = two_sided_book(10 * S)
    # A state at exactly the source instant must not become its own label.
    events.append(ev(10 * S, "A", "B", 100 * PX + 10**7, 100, 3, 3))
    events.append(ev(11 * S, "A", "B", 100 * PX + 2 * 10**7, 100, 4, 4))
    row = resolve([source(10 * S, 100.5 * PX)], events)[0]
    assert row["next_change_label_ts_event"] > 10 * S


def test_multiple_cadences_resolve_independently_on_one_replay():
    events = two_sided_book(10 * S)
    for i in range(1, 1_200):
        events.append(
            ev(10 * S + i * 10 * MS, "A", "B", (100 * PX) + (i % 7) * 10**7, 100, 1000 + i, 1000 + i)
        )
    sources = [
        source(10 * S + 1 * S, 100.5 * PX, cadence="1s", index=1),
        source(10 * S + 1 * S, 100.5 * PX, cadence="5s", index=0),
        source(10 * S + 2 * S, 100.5 * PX, cadence="50ev", index=3),
    ]
    rows = resolve(sources, events)
    assert len(rows) == 3
    assert {r["cadence"] for r in rows} == {"1s", "5s", "50ev"}
    # Two sources at the same instant get the same label instants.
    at_one_second = [r for r in rows if r["source_ts_event"] == 10 * S + 1 * S]
    assert len(at_one_second) == 2
    assert (
        at_one_second[0]["h1s_label_ts_event"] == at_one_second[1]["h1s_label_ts_event"]
    )


# ---------------------------------------------------------------------------
# Coverage reporting
# ---------------------------------------------------------------------------


def test_status_summary_reports_every_horizon_and_no_relationship():
    events = two_sided_book(10 * S)
    for i in range(1, 300):
        events.append(
            ev(10 * S + i * 40 * MS, "A", "B", (100 * PX) + (i % 3) * 10**7, 100, 1000 + i, 1000 + i)
        )
    sources = [source(10 * S + i * 500 * MS, 100.5 * PX, index=i) for i in range(8)]
    summary = label_status_summary(resolve(sources, events))
    assert summary["rows"] == 8
    assert set(summary["by_horizon"]) == set(HORIZON_NAMES)
    assert summary["contains_predictive_result"] is False
    for counts in summary["by_horizon"].values():
        assert sum(counts.values()) == 8


# ---------------------------------------------------------------------------
# Integration: real MBO file + real Stage-1 features
# ---------------------------------------------------------------------------

CMCSA_FILE = "xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst"


def _find_cmcsa_file():
    import os

    override = os.environ.get("KEFTRADE_MBO_TEST_FILE")
    if override and Path(override).is_file():
        return Path(override)
    for root in (
        Path(__file__).resolve().parents[3] / "data" / "databento",
        Path(__file__).resolve().parents[3],
        Path("/opt/keftrade/data/databento"),
        Path("/opt/keftrade"),
    ):
        candidate = root / CMCSA_FILE
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.skipif(
    _find_cmcsa_file() is None,
    reason=f"{CMCSA_FILE} not present; set KEFTRADE_MBO_TEST_FILE to run",
)
def test_real_mbo_stream_labels_real_stage1_snapshots(tmp_path):
    """One raw symbol-day, Stage-1 features built from it, labels resolved from
    the same certified stream in a single replay.

    Asserts structure and causality only. No predictive quantity is computed.
    """
    from app.services.mbo_book_validator import iter_dbn_events
    from app.services.mbo_feature_engine import CADENCES
    from app.services.mbo_feature_store import write_session_features
    from app.services.mbo_label_engine import read_source_snapshots

    path = _find_cmcsa_file()
    features_dir = tmp_path / "features"
    manifest = write_session_features(
        iter_dbn_events(str(path)),
        symbol="CMCSA",
        session_date="2025-06-26",
        output_dir=features_dir,
        source_path=path,
        cadences=CADENCES,
    )
    feature_paths = [
        str(features_dir / manifest["cadences"][c.name]["path"]) for c in CADENCES
    ]
    before = {p: Path(p).read_bytes() for p in feature_paths}

    symbol, session_date, sources = read_source_snapshots(feature_paths)
    assert symbol == "CMCSA"
    assert session_date == "2025-06-26"
    assert len(sources) == manifest["total_rows"]
    # Sorted by event time, which the streaming resolver requires.
    assert [s.ts_event for s in sources] == sorted(s.ts_event for s in sources)

    rows = resolve_symbol_day_labels(
        symbol=symbol,
        session_date=session_date,
        sources=sources,
        events=iter_dbn_events(str(path)),
    )
    assert len(rows) == len(sources), "wide storage: one row per source snapshot"

    summary = label_status_summary(rows)
    assert summary["rows"] == len(sources)
    # Short horizons must resolve far more often than a 60-second one.
    assert (
        summary["by_horizon"]["1s"][LABEL_OK] >= summary["by_horizon"]["60s"][LABEL_OK]
    )
    assert summary["by_horizon"]["next_change"][LABEL_OK] > 0

    for row in rows:
        for horizon in HORIZONS:
            prefix = horizon.prefix
            if row[f"{prefix}_status"] != LABEL_OK:
                continue
            assert row[f"{prefix}_label_ts_event"] > row["source_ts_event"]
            if horizon.kind == "time":
                assert (
                    row[f"{prefix}_label_ts_event"]
                    >= row[f"{prefix}_target_ts_event"]
                )
                assert row[f"{prefix}_realized_lag_ns"] >= 0
            assert (
                row[f"{prefix}_available_ts_recv"]
                >= row["source_feature_available_ts_recv"]
            )

    # The frozen feature dataset is untouched.
    for parquet_path, payload in before.items():
        assert Path(parquet_path).read_bytes() == payload
