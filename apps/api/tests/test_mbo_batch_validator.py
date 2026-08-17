"""Tier-1 all-session gate: aggregation, matrix, and failure handling.

No real DBN files are needed. `iter_dbn_events` is substituted with synthetic
event streams keyed by filename, so the batch walks real paths through the real
replay core and the real aggregation, and only the bytes-on-disk step is faked.
"""

from __future__ import annotations

import csv
import json

import pytest

from app.services import mbo_batch_validator as batch_module
from app.services.mbo_batch_validator import (
    EXPECTED_MBO_FILE_COUNT,
    MATRIX_COLUMNS,
    aggregate,
    discover_files,
    matrix_row,
    parse_symbol_date,
    run_batch,
    write_matrix_csv,
)
from app.services.mbo_book_validator import (
    F_BAD_TS_RECV,
    F_LAST,
    F_MAYBE_BAD_BOOK,
    FIXED_PRICE_SCALE,
    INIT_KNOWN_EMPTY_CLEAR,
    INIT_UNKNOWN,
    MboEvent,
    replay,
    validation_report,
)

PX = FIXED_PRICE_SCALE


def event(action, side="B", price=0, size=0, order_id=0, *, seq, ts, flags=F_LAST):
    return MboEvent(
        ts_event=ts,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        flags=flags,
        sequence=seq,
    )


def opening():
    """The real XNAS session start: a sequence-0 `R` clear."""
    return event("R", "N", 0, 0, 0, seq=0, ts=0, flags=F_BAD_TS_RECV)


def clean_session():
    return [
        opening(),
        event("A", "B", 100 * PX, 500, 1, seq=1, ts=1_000),
        event("A", "A", 101 * PX, 300, 2, seq=2, ts=2_000),
        event("C", "B", 100 * PX, 200, 1, seq=3, ts=3_000),
    ]


def crossed_session():
    """A genuine reconstruction failure: bid above ask at an event boundary."""
    return [
        opening(),
        event("A", "B", 102 * PX, 100, 1, seq=1, ts=1_000),
        event("A", "A", 101 * PX, 100, 2, seq=2, ts=2_000),
    ]


def maybe_bad_book_session():
    return [
        opening(),
        event("A", "B", 100 * PX, 500, 1, seq=1, ts=1_000),
        event(
            "A", "A", 101 * PX, 300, 2, seq=2, ts=2_000, flags=F_LAST | F_MAYBE_BAD_BOOK
        ),
    ]


def unknown_init_session():
    """Starts on an order mutation: state we were never given."""
    return [
        event("A", "B", 100 * PX, 500, 1, seq=1, ts=1_000),
        event("A", "A", 101 * PX, 300, 2, seq=2, ts=2_000),
    ]


def make_batch(tmp_path, sessions: dict[str, list], unreadable: tuple[str, ...] = ()):
    """Write placeholder files and route each name to a synthetic stream."""
    directory = tmp_path / "dbn"
    directory.mkdir()
    for name in list(sessions) + list(unreadable):
        (directory / name).write_bytes(b"placeholder")

    def fake_iter(path_str):
        name = path_str.replace("\\", "/").rsplit("/", 1)[-1]
        if name in unreadable:
            raise OSError(f"zstd frame corrupt in {name}")
        yield from sessions[name]

    return directory, fake_iter


def name_for(symbol: str, date: str = "20250626") -> str:
    return f"xnas-itch-{date}.mbo.{symbol}.0000.dbn.zst"


# ---------------------------------------------------------------------------
# Filename parsing and discovery
# ---------------------------------------------------------------------------


def test_symbol_and_date_are_parsed_from_the_databento_filename():
    assert parse_symbol_date("xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst") == (
        "CMCSA",
        "2025-06-26",
    )


def test_an_unparseable_name_is_reported_as_none_rather_than_guessed():
    assert parse_symbol_date("something-else.dbn.zst") == (None, None)


def test_discovery_finds_dbn_files_in_a_stable_order(tmp_path):
    directory, _ = make_batch(
        tmp_path, {name_for("BBB"): [], name_for("AAA"): [], name_for("CCC"): []}
    )
    found = [path.name for path in discover_files(directory)]
    assert found == sorted(found)
    assert len(found) == 3


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregation_of_clean_files_certifies(tmp_path, monkeypatch):
    sessions = {name_for(sym): clean_session() for sym in ("AAA", "BBB", "CCC")}
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, rows = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=3
    )
    assert summary["files_discovered"] == 3
    assert summary["files_completed"] == 3
    assert summary["files_failed_to_read"] == 0
    assert summary["files_initialization_uncertified"] == 0
    assert summary["files_integrity_uncertified"] == 0
    assert summary["files_not_clean"] == 0
    assert summary["total_f_maybe_bad_book_records"] == 0
    assert summary["total_fatal_violations"] == 0
    assert summary["overall_certified"] is True
    assert all(row["initialization_mode"] == INIT_KNOWN_EMPTY_CLEAR for row in rows)
    assert summary["total_records"] == 12
    assert summary["total_book_states_checked"] > 0


def test_one_dirty_file_fails_the_aggregate(tmp_path, monkeypatch):
    sessions = {
        name_for("AAA"): clean_session(),
        name_for("BBB"): crossed_session(),
        name_for("CCC"): clean_session(),
    }
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, _ = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=3
    )
    assert summary["overall_certified"] is False
    assert summary["files_not_clean"] == 1
    assert summary["files_integrity_uncertified"] == 1
    assert summary["gate_checks"]["all_clean"] is False
    assert summary["gate_checks"]["no_fatal_violations"] is False
    assert summary["failing_files"]["not_clean"] == [name_for("BBB")]
    # The other two still completed: one bad session is not a batch abort.
    assert summary["files_completed"] == 3


def test_one_maybe_bad_book_fails_aggregate_certification(tmp_path, monkeypatch):
    sessions = {
        name_for("AAA"): clean_session(),
        name_for("BBB"): maybe_bad_book_session(),
    }
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, _ = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=2
    )
    assert summary["total_f_maybe_bad_book_records"] == 1
    assert summary["gate_checks"]["no_maybe_bad_book"] is False
    assert summary["overall_certified"] is False
    assert name_for("BBB") in summary["failing_files"]["integrity_uncertified"]


def test_unknown_initialization_fails_the_aggregate(tmp_path, monkeypatch):
    sessions = {
        name_for("AAA"): clean_session(),
        name_for("BBB"): unknown_init_session(),
    }
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, rows = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=2
    )
    assert summary["files_initialization_uncertified"] == 1
    assert summary["gate_checks"]["all_initializations_certified"] is False
    assert summary["overall_certified"] is False
    bad = next(row for row in rows if row["source"] == name_for("BBB"))
    assert bad["initialization_mode"] == INIT_UNKNOWN
    assert bad["initialization_certified"] is False


def test_batch_continues_after_one_file_fails_to_read(tmp_path, monkeypatch):
    sessions = {name_for("AAA"): clean_session(), name_for("CCC"): clean_session()}
    directory, fake_iter = make_batch(tmp_path, sessions, unreadable=(name_for("BBB"),))
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, rows = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=3
    )
    assert summary["files_discovered"] == 3
    assert summary["files_completed"] == 2, "the readable files must still be replayed"
    assert summary["files_failed_to_read"] == 1
    assert summary["gate_checks"]["no_unreadable_files"] is False
    assert summary["overall_certified"] is False
    assert summary["failing_files"]["unreadable"] == [name_for("BBB")]

    failed = next(row for row in rows if row["source"] == name_for("BBB"))
    assert "zstd frame corrupt" in failed["read_error"]
    assert failed["certified"] is False and failed["clean"] is False
    # The unreadable file still occupies a row, so the denominator is honest.
    assert len(rows) == 3


def test_expected_file_count_mismatch_fails_even_when_every_file_is_clean(
    tmp_path, monkeypatch
):
    """159 clean sessions is not 'nearly certified'; it is a short batch."""
    sessions = {name_for(f"S{i:03d}"): clean_session() for i in range(3)}
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, _ = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=4
    )
    assert summary["files_completed"] == 3
    assert summary["files_not_clean"] == 0
    assert summary["gate_checks"]["expected_file_count_met"] is False
    assert summary["overall_certified"] is False


def test_the_frozen_experiment_expects_one_hundred_and_sixty_files():
    assert EXPECTED_MBO_FILE_COUNT == 160


# ---------------------------------------------------------------------------
# Counts and matrix
# ---------------------------------------------------------------------------


def test_matrix_counts_are_uncapped(tmp_path, monkeypatch):
    """Fifty unknown-order cancels must report fifty, not the sample limit."""
    session = [opening()]
    session.extend(
        event("C", "B", 100 * PX, 1, 900_000 + i, seq=i + 1, ts=(i + 1) * 1_000)
        for i in range(50)
    )
    sessions = {name_for("AAA"): session}
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    summary, rows = run_batch(
        directory, output_dir=tmp_path / "out", expected_file_count=1
    )
    assert rows[0]["unknown_order_cancel"] == 50
    assert summary["total_fatal_violations"] == 50
    assert summary["fatal_violations_by_kind"]["unknown_order_cancel"] == 50
    assert summary["overall_certified"] is False


def test_matrix_row_carries_every_declared_column():
    book, state, violations = replay(clean_session())
    report = validation_report(book, state, violations, source=name_for("CMCSA"))
    row = matrix_row(report, source=name_for("CMCSA"))
    assert set(MATRIX_COLUMNS) - set(row) == set()
    assert row["symbol"] == "CMCSA"
    assert row["date"] == "2025-06-26"
    # All seven actions are present, absent ones as explicit zeros.
    assert row["action_A"] == 2
    assert row["action_C"] == 1
    assert row["action_R"] == 1
    assert row["action_M"] == 0
    assert row["action_F"] == 0
    assert row["action_T"] == 0
    assert row["action_N"] == 0


def test_csv_matrix_is_written_with_one_row_per_file(tmp_path, monkeypatch):
    sessions = {name_for(sym): clean_session() for sym in ("AAA", "BBB")}
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    output = tmp_path / "out"
    _, rows = run_batch(directory, output_dir=output, expected_file_count=2)
    csv_path = output / "matrix.csv"
    write_matrix_csv(rows, csv_path)

    with csv_path.open(newline="", encoding="utf-8") as handle:
        parsed = list(csv.DictReader(handle))
    assert len(parsed) == 2
    assert list(parsed[0]) == list(MATRIX_COLUMNS)
    assert {row["symbol"] for row in parsed} == {"AAA", "BBB"}


def test_per_file_json_is_written_for_each_completed_file(tmp_path, monkeypatch):
    sessions = {name_for(sym): clean_session() for sym in ("AAA", "BBB")}
    directory, fake_iter = make_batch(tmp_path, sessions)
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)

    output = tmp_path / "out"
    run_batch(directory, output_dir=output, expected_file_count=2)
    written = sorted(path.name for path in (output / "per_file").glob("*.json"))
    assert len(written) == 2
    payload = json.loads((output / "per_file" / written[0]).read_text(encoding="utf-8"))
    assert payload["initialization"]["mode"] == INIT_KNOWN_EMPTY_CLEAR
    assert payload["integrity"]["certified"] is True


def test_aggregate_is_a_conjunction_not_a_score():
    """Every clause must hold; one failing clause is enough."""
    clean = {
        "read_error": None,
        "initialization_certified": True,
        "certified": True,
        "clean": True,
        "records": 10,
        "book_states_checked": 5,
        "f_maybe_bad_book_records": 0,
        "f_bad_ts_recv_live_records": 0,
        "source": "ok.dbn.zst",
    }
    passing = aggregate([clean], files_discovered=1, expected_file_count=1)
    assert passing["overall_certified"] is True

    for field, value in (
        ("initialization_certified", False),
        ("certified", False),
        ("clean", False),
    ):
        summary = aggregate(
            [{**clean, field: value}], files_discovered=1, expected_file_count=1
        )
        assert summary["overall_certified"] is False, field


def test_bad_ts_recv_is_totalled_but_never_gates():
    row = {
        "read_error": None,
        "initialization_certified": True,
        "certified": True,
        "clean": True,
        "records": 10,
        "book_states_checked": 5,
        "f_maybe_bad_book_records": 0,
        "f_bad_ts_recv_live_records": 7,
        "source": "ok.dbn.zst",
    }
    summary = aggregate([row], files_discovered=1, expected_file_count=1)
    assert summary["total_f_bad_ts_recv_live_records"] == 7
    assert summary["overall_certified"] is True


@pytest.mark.parametrize("column", ["symbol", "date", "initialization_mode", "certified"])
def test_failed_rows_still_carry_identity(column, tmp_path, monkeypatch):
    directory, fake_iter = make_batch(tmp_path, {}, unreadable=(name_for("ZZZ"),))
    monkeypatch.setattr(batch_module, "iter_dbn_events", fake_iter)
    _, rows = run_batch(directory, output_dir=tmp_path / "out", expected_file_count=1)
    assert rows[0][column] is not None
