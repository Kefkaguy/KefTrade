"""Stage 2A: label semantics, session edges, and label-side leakage.

No feature-label relationship is computed anywhere in this file. These tests pin
what a label *is*, not whether it is predictable.
"""

from __future__ import annotations

import pytest

from app.services.mbo_label_engine import (
    HORIZON_NAMES,
    HORIZONS,
    LABEL_COLUMNS,
    LABEL_DEFINITION_HASH,
    LABEL_NO_FURTHER_MIDPOINT_CHANGE,
    LABEL_NO_VALID_FUTURE_STATE,
    LABEL_OK,
    LABEL_SESSION_END_BEFORE_HORIZON,
    LABEL_SOURCE_MIDPOINT_UNAVAILABLE,
    LABEL_STATUSES,
    NANOS_PER_SECOND,
    REQUIRED_FEATURE_SEMANTICS_HASH,
    Horizon,
    SnapshotSpine,
    build_labels,
    label_definitions,
    label_status_summary,
)

S = NANOS_PER_SECOND


def spine(midpoints, *, step_ns=S, start=10 * S, cadence="1s", recv_offset=1_000):
    """A synthetic snapshot spine on a regular grid.

    `None` in `midpoints` marks an incoherent state -- one side of the touch
    empty -- which can be neither a source nor a label.
    """
    n = len(midpoints)
    ts = [start + i * step_ns for i in range(n)]
    return SnapshotSpine(
        symbol="TEST",
        session_date="2025-06-26",
        cadence=cadence,
        sequence_index=list(range(n)),
        ts_event=ts,
        grid_ts_event=list(ts),
        ts_recv=[t + recv_offset for t in ts],
        feature_available_ts_recv=[t + recv_offset for t in ts],
        midpoint=list(midpoints),
    )


def labels(spine_obj, horizons=HORIZONS):
    return list(build_labels(spine_obj, horizons))


def only(rows, horizon, sequence_index):
    return next(
        r for r in rows if r["horizon"] == horizon and r["sequence_index"] == sequence_index
    )


ONE_SEC = (Horizon("1s", "time", S),)
NEXT = (Horizon("next_change", "changes", 1),)
NEXT2 = (Horizon("next_2_changes", "changes", 2),)


# ---------------------------------------------------------------------------
# Frozen declaration
# ---------------------------------------------------------------------------


def test_seven_horizons_are_frozen_together():
    assert HORIZON_NAMES == (
        "next_change",
        "next_2_changes",
        "1s",
        "5s",
        "10s",
        "30s",
        "60s",
    )


def test_definitions_name_the_feature_artefact_they_were_built_against():
    definitions = label_definitions()
    assert definitions["built_against"]["feature_semantics_hash"] == (
        REQUIRED_FEATURE_SEMANTICS_HASH
    )
    assert definitions["built_against"]["features_modified"] is False
    assert definitions["contains_predictive_result"] is False
    assert definitions["label_definition_hash"] == LABEL_DEFINITION_HASH


def test_every_label_row_carries_the_declared_columns():
    rows = labels(spine([100.0, 100.5, 101.0]))
    assert rows
    for row in rows:
        assert set(row) == set(LABEL_COLUMNS)
        assert row["label_status"] in LABEL_STATUSES


# ---------------------------------------------------------------------------
# Time horizons: the at-or-after rule
# ---------------------------------------------------------------------------


def test_time_label_takes_the_first_state_at_or_after_the_target():
    rows = labels(spine([100.0, 101.0, 102.0]), ONE_SEC)
    first = only(rows, "1s", 0)
    assert first["label_status"] == LABEL_OK
    assert first["target_ts_event"] == 11 * S
    assert first["label_ts_event"] == 11 * S
    assert first["realized_lag_ns"] == 0
    assert first["future_midpoint"] == 101.0
    assert first["midpoint_change"] == pytest.approx(1.0)
    assert first["return_bps"] == pytest.approx(1.0 / 100.0 * 10_000)


def test_a_state_exactly_at_the_target_is_used():
    rows = labels(spine([100.0, 100.0, 103.0], step_ns=S), ONE_SEC)
    assert only(rows, "1s", 0)["label_ts_event"] == 11 * S


def test_a_sparse_grid_takes_the_next_state_after_the_target_and_records_the_lag():
    """The horizon is never shortened to fit the data."""
    rows = labels(spine([100.0, 105.0], step_ns=7 * S), ONE_SEC)
    row = only(rows, "1s", 0)
    assert row["target_ts_event"] == 11 * S
    assert row["label_ts_event"] == 17 * S
    assert row["realized_lag_ns"] == 6 * S
    assert row["future_midpoint"] == 105.0


def test_incoherent_future_states_are_skipped_and_counted():
    rows = labels(spine([100.0, None, None, 104.0]), ONE_SEC)
    row = only(rows, "1s", 0)
    assert row["label_status"] == LABEL_OK
    assert row["skipped_incoherent_states"] == 2
    assert row["label_ts_event"] == 13 * S
    assert row["future_midpoint"] == 104.0


def test_no_state_after_the_target_is_missing_not_backfilled():
    """The last snapshot of a session has no 1s label. It is not given one."""
    rows = labels(spine([100.0, 101.0]), ONE_SEC)
    last = only(rows, "1s", 1)
    assert last["label_status"] == LABEL_SESSION_END_BEFORE_HORIZON
    assert last["future_midpoint"] is None
    assert last["return_bps"] is None
    assert last["label_ts_event"] is None


def test_a_future_that_never_becomes_coherent_is_named_distinctly():
    rows = labels(spine([100.0, None, None]), ONE_SEC)
    row = only(rows, "1s", 0)
    assert row["label_status"] == LABEL_NO_VALID_FUTURE_STATE


def test_an_incoherent_source_yields_no_label_at_any_horizon():
    rows = labels(spine([None, 101.0, 102.0]))
    for horizon in HORIZON_NAMES:
        row = only(rows, horizon, 0)
        assert row["label_status"] == LABEL_SOURCE_MIDPOINT_UNAVAILABLE
        assert row["future_midpoint"] is None


def test_longer_horizons_run_out_before_shorter_ones():
    """A 60s label needs 60 seconds; a 1s label needs one. Neither borrows."""
    rows = labels(spine([100.0 + i for i in range(12)]))
    summary = label_status_summary(rows)
    ok_1s = summary["by_horizon"]["1s"][LABEL_OK]
    ok_60s = summary["by_horizon"]["60s"][LABEL_OK]
    assert ok_1s > ok_60s
    assert ok_60s == 0, "an 11-second session cannot carry a 60-second label"


# ---------------------------------------------------------------------------
# Change-count horizons
# ---------------------------------------------------------------------------


def test_next_change_skips_snapshots_whose_midpoint_is_unchanged():
    rows = labels(spine([100.0, 100.0, 100.0, 101.0]), NEXT)
    row = only(rows, "next_change", 0)
    assert row["label_sequence_index"] == 3
    assert row["future_midpoint"] == 101.0
    assert row["midpoint_change"] == pytest.approx(1.0)
    # A change horizon has no target instant.
    assert row["target_ts_event"] is None
    assert row["realized_lag_ns"] is None


def test_next_two_changes_finds_the_second_distinct_move():
    rows = labels(spine([100.0, 100.0, 101.0, 101.0, 99.0]), NEXT2)
    row = only(rows, "next_2_changes", 0)
    assert row["label_sequence_index"] == 4
    assert row["future_midpoint"] == 99.0
    assert row["midpoint_change"] == pytest.approx(-1.0)


def test_next_two_changes_is_missing_when_only_one_move_remains():
    rows = labels(spine([100.0, 101.0]), NEXT2)
    assert only(rows, "next_2_changes", 0)["label_status"] == (
        LABEL_NO_FURTHER_MIDPOINT_CHANGE
    )


def test_a_flat_session_has_no_change_labels_at_all():
    rows = labels(spine([100.0] * 8), NEXT)
    statuses = {row["label_status"] for row in rows}
    assert statuses == {LABEL_NO_FURTHER_MIDPOINT_CHANGE}


def test_change_horizons_skip_incoherent_states_rather_than_treating_them_as_changes():
    rows = labels(spine([100.0, None, 100.0, 102.0]), NEXT)
    row = only(rows, "next_change", 0)
    assert row["label_sequence_index"] == 3, "a gap is not a midpoint change"
    assert row["future_midpoint"] == 102.0


# ---------------------------------------------------------------------------
# Session edges
# ---------------------------------------------------------------------------


def test_labels_never_leave_the_symbol_day():
    """Two sessions labelled separately produce no cross-session label.

    An overnight gap is not a 60-second horizon.
    """
    monday = labels(spine([100.0, 101.0, 102.0]), ONE_SEC)
    tuesday_spine = spine([200.0, 201.0, 202.0], start=90_000 * S)
    tuesday_spine.session_date = "2025-06-27"
    tuesday = labels(tuesday_spine, ONE_SEC)

    for row in monday:
        assert row["session_date"] == "2025-06-26"
        if row["label_ts_event"] is not None:
            assert row["label_ts_event"] < 90_000 * S
    assert only(monday, "1s", 2)["label_status"] == LABEL_SESSION_END_BEFORE_HORIZON
    for row in tuesday:
        assert row["session_date"] == "2025-06-27"


# ---------------------------------------------------------------------------
# Provenance and availability
# ---------------------------------------------------------------------------


def test_label_availability_never_precedes_the_feature_row_or_the_label_state():
    rows = labels(spine([100.0 + i for i in range(20)]))
    for row in rows:
        if row["label_status"] != LABEL_OK:
            continue
        assert row["label_available_ts_recv"] >= row["source_feature_available_ts_recv"]
        assert row["label_available_ts_recv"] >= row["label_ts_recv"]
        # A label is always strictly in the future of its source.
        assert row["label_ts_event"] > row["source_ts_event"]


def test_source_and_label_timestamps_are_both_preserved():
    rows = labels(spine([100.0, 101.0]), ONE_SEC)
    row = only(rows, "1s", 0)
    assert row["source_ts_event"] == 10 * S
    assert row["source_grid_ts_event"] == 10 * S
    assert row["source_midpoint"] == 100.0
    assert row["label_ts_event"] == 11 * S
    assert row["label_ts_recv"] == 11 * S + 1_000


# ---------------------------------------------------------------------------
# Leakage: the future cannot change an earlier label
# ---------------------------------------------------------------------------


def test_appending_future_snapshots_cannot_change_earlier_labels():
    """Truncation invariance on the label side.

    Extending a session must not alter any label that was already resolvable,
    which is the label-side analogue of the Stage-1 feature invariance.
    """
    short = spine([100.0, 101.0, 102.0, 103.0, 104.0])
    long = spine([100.0, 101.0, 102.0, 103.0, 104.0, 999.0, -999.0, 500.0])

    short_rows = {
        (r["horizon"], r["sequence_index"]): r
        for r in labels(short)
        if r["label_status"] == LABEL_OK
    }
    long_rows = {
        (r["horizon"], r["sequence_index"]): r for r in labels(long)
    }
    assert short_rows, "the fixture must resolve some labels"
    for key, row in short_rows.items():
        assert long_rows[key] == row, f"{key} changed when the future was extended"


def test_perturbing_the_far_future_cannot_change_a_resolved_label():
    base_mids = [100.0 + i * 0.5 for i in range(30)]
    original = labels(spine(base_mids))
    perturbed_mids = list(base_mids)
    for index in range(20, 30):
        perturbed_mids[index] = 5_000.0 + index
    perturbed = labels(spine(perturbed_mids))

    def resolved_before(rows, cutoff_ts):
        return {
            (r["horizon"], r["sequence_index"]): r
            for r in rows
            if r["label_status"] == LABEL_OK and r["label_ts_event"] < cutoff_ts
        }

    cutoff = 10 * S + 20 * S
    before = resolved_before(original, cutoff)
    after = resolved_before(perturbed, cutoff)
    assert before, "the fixture must resolve labels before the cutoff"
    assert before == after


def test_a_label_never_reads_a_state_before_its_own_target():
    rows = labels(spine([100.0 + i for i in range(40)]))
    for row in rows:
        if row["label_status"] != LABEL_OK or row["horizon_kind"] != "time":
            continue
        assert row["label_ts_event"] >= row["target_ts_event"]
        assert row["realized_lag_ns"] >= 0


def test_no_horizon_is_substituted_for_another():
    """Every horizon resolves independently; none inherits another's label."""
    rows = labels(spine([100.0 + i for i in range(80)]))
    by_horizon = {}
    for row in rows:
        if row["sequence_index"] == 0 and row["label_status"] == LABEL_OK:
            by_horizon[row["horizon"]] = row["label_ts_event"]
    for name, magnitude in (("1s", 1), ("5s", 5), ("10s", 10), ("30s", 30)):
        assert by_horizon[name] == 10 * S + magnitude * S, name
    # Distinct horizons must not collapse onto one label instant.
    time_labels = [by_horizon[n] for n in ("1s", "5s", "10s", "30s")]
    assert len(set(time_labels)) == len(time_labels)


# ---------------------------------------------------------------------------
# Coverage reporting
# ---------------------------------------------------------------------------


def test_labels_read_from_a_real_stage1_parquet_without_modifying_it(tmp_path):
    """End to end against the actual frozen artefact shape.

    Writes a Stage-1 feature file with the real engine, reads the labelling
    spine back out of it, and asserts the feature file is byte-identical
    afterwards -- Stage 2A must not touch the frozen dataset.
    """
    from app.services.mbo_book_validator import F_BAD_TS_RECV, F_LAST, MboEvent
    from app.services.mbo_feature_engine import Cadence
    from app.services.mbo_feature_store import write_session_features
    from app.services.mbo_label_engine import read_spine

    px = 1_000_000_000
    events = [
        MboEvent(
            ts_event=10 * S,
            action="R",
            side="N",
            price=0,
            size=0,
            order_id=0,
            flags=F_BAD_TS_RECV,
            sequence=0,
            ts_recv=10 * S + 1,
        )
    ]
    seq = 1
    for step in range(60):
        ts = 10 * S + step * 400_000_000
        side = "B" if step % 2 else "A"
        # A drifting touch, so midpoints actually change and change-horizons
        # have something to find.
        price = (100 * px - (step % 5) * 10**7) if side == "B" else (
            101 * px + (step % 5) * 10**7
        )
        events.append(
            MboEvent(
                ts_event=ts,
                action="A",
                side=side,
                price=price,
                size=100,
                order_id=seq,
                flags=F_LAST,
                sequence=seq,
                ts_recv=ts + 1_000,
            )
        )
        seq += 1

    features_dir = tmp_path / "features"
    manifest = write_session_features(
        events,
        symbol="TEST",
        session_date="2025-06-26",
        output_dir=features_dir,
        cadences=(Cadence("1s", "time", S),),
    )
    feature_path = features_dir / manifest["cadences"]["1s"]["path"]
    before = feature_path.read_bytes()

    spine_obj = read_spine(str(feature_path))
    assert spine_obj.symbol == "TEST"
    assert spine_obj.session_date == "2025-06-26"
    assert spine_obj.cadence == "1s"
    assert len(spine_obj) == manifest["cadences"]["1s"]["rows"]

    rows = list(build_labels(spine_obj))
    assert len(rows) == len(spine_obj) * len(HORIZONS)
    resolved = [r for r in rows if r["label_status"] == LABEL_OK]
    assert resolved, "a 24-second session must resolve some labels"
    for row in resolved:
        assert row["label_ts_event"] > row["source_ts_event"]
        assert row["label_available_ts_recv"] >= row["source_feature_available_ts_recv"]

    # The frozen dataset is untouched.
    assert feature_path.read_bytes() == before


def test_status_summary_reports_every_horizon_and_never_a_relationship():
    rows = labels(spine([100.0 + (i % 3) for i in range(15)]))
    summary = label_status_summary(rows)
    assert set(summary["by_horizon"]) == set(HORIZON_NAMES)
    assert summary["contains_predictive_result"] is False
    for name in HORIZON_NAMES:
        assert sum(summary["by_horizon"][name].values()) == 15
