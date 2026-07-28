from __future__ import annotations

from typing import Any

import pytest

from app.services import research_champion_import as rci


def candidate_payload(*, blocks="momentum_rsi_55", parent="root_a", direction="long"):
    return {"candidate_id": "unused", "blocks": {"entry": blocks}, "parent_candidate_id": parent, "parameters": {"direction": direction}}


def promoted_job(
    *,
    job_id,
    campaign_id,
    candidate_id,
    symbol="AMD",
    timeframe="30m",
    family="session_momentum",
    blocks="momentum_rsi_55",
    parent="root_a",
    pf=1.7,
    trades=84,
    dd=0.04,
):
    return {
        "id": job_id,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_family": family,
        "family_id": family,
        "parent_candidate_id": parent,
        "dataset_id": None,
        "hypothesis_version_id": None,
        "campaign_name": f"campaign-{campaign_id}",
        "candidate": candidate_payload(blocks=blocks, parent=parent),
        "result": {"metrics": {"profit_factor": pf, "expectancy_per_trade": 6.0, "max_drawdown": dd, "number_of_trades": trades}},
        "validation_score": 0.8,
    }


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeImportConnection:
    """Dispatches on distinguishing query fragments -- see module docstring
    equivalent note above each branch for which real query it stands in for."""

    def __init__(self, *, promoted_jobs=None, existing_champion_rows=None, champion_rows_for_dedupe=None):
        self.promoted_jobs = promoted_jobs or []
        self.existing_champion_rows = existing_champion_rows or []
        self.champion_rows_for_dedupe = champion_rows_for_dedupe or []
        self.inserted: list[tuple[Any, ...]] = []
        self.demote_calls: list[tuple[str, list[int]]] = []
        self.commits = 0
        self._next_id = 1000

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        if "FROM research_campaign_jobs j" in text and "JOIN research_campaigns c" in text:
            return FakeResult(list(self.promoted_jobs))
        if "e.promotion_state = ANY(%s)" in text:
            states = set(params[0])
            return FakeResult([row for row in self.existing_champion_rows if row["promotion_state"] in states])
        if "e.promotion_state = 'research_champion'" in text and "elite_champion_validation" not in text:
            return FakeResult(list(self.champion_rows_for_dedupe))
        if text.startswith("INSERT INTO elite_research_candidates("):
            self.inserted.append(params)
            self._next_id += 1
            return FakeResult([{"id": self._next_id}])
        if "COUNT(*) AS eligible_promoted_jobs" in text:
            return FakeResult([{"eligible_promoted_jobs": 0, "symbols": 0, "timeframes": 0, "families": 0}])
        if "COUNT(*) FILTER (WHERE promotion_state = 'research_champion')" in text:
            return FakeResult([{}])
        if text.startswith("UPDATE elite_research_candidates"):
            self.demote_calls.append((params[0], list(params[1])))
            return FakeResult([])
        raise AssertionError(f"unexpected query in FakeImportConnection: {text[:120]}")

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _stub_ensure_campaign_tables(monkeypatch):
    from app.services import research_campaigns

    monkeypatch.setattr(research_campaigns, "ensure_campaign_tables", lambda conn: None)


def champion_row(job, *, candidate_id="cand_original", campaign_id=10, state="research_champion"):
    return {
        "candidate_id": candidate_id,
        "campaign_id": campaign_id,
        "symbol": job["symbol"],
        "timeframe": job["timeframe"],
        "candidate": job["candidate"],
        "strategy_family": job["strategy_family"],
        "job_family_id": job["family_id"],
        "job_parent_candidate_id": job["parent_candidate_id"],
        "promotion_state": state,
    }


# ---------------------------------------------------------------------------
# The backlog count and the import must mean the same thing
# ---------------------------------------------------------------------------

def test_the_backlog_counts_only_what_the_import_can_actually_add() -> None:
    """The reported bug: the backlog counted job rows not yet imported by
    (campaign_id, candidate_id), while the import additionally dropped rows
    whose strategy a live champion already covered. The count promised 15 and
    the button delivered 0, forever."""
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    duplicates = [
        promoted_job(job_id=index, campaign_id=20, candidate_id=f"cand_new_{index}")
        for index in range(2, 6)
    ]
    conn = FakeImportConnection(
        promoted_jobs=duplicates,
        existing_champion_rows=[champion_row(existing)],
    )

    status = rci.research_champion_status(conn)

    assert status["eligible_promoted_jobs"] == 0
    assert status["eligible_jobs_scanned"] == 4
    assert status["duplicate_of_existing_champion"] == 4


def test_the_backlog_counts_a_genuinely_new_strategy() -> None:
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    fresh = promoted_job(job_id=2, campaign_id=20, candidate_id="cand_new", symbol="NVDA")
    conn = FakeImportConnection(
        promoted_jobs=[fresh],
        existing_champion_rows=[champion_row(existing)],
    )

    status = rci.research_champion_status(conn)

    assert status["eligible_promoted_jobs"] == 1
    assert status["duplicate_of_existing_champion"] == 0


def test_the_backlog_collapses_duplicates_within_itself() -> None:
    """Four job rows of one strategy are one importable champion, so the
    backlog must say 1 rather than 4."""
    rows = [
        promoted_job(job_id=index, campaign_id=20, candidate_id=f"cand_{index}")
        for index in range(1, 5)
    ]
    conn = FakeImportConnection(promoted_jobs=rows)

    status = rci.research_champion_status(conn)

    assert status["eligible_promoted_jobs"] == 1
    assert status["duplicate_within_backlog"] == 3


def test_the_count_and_the_import_agree() -> None:
    """The invariant that was broken: whatever the backlog reports is exactly
    what a subsequent import inserts."""
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    rows = [
        promoted_job(job_id=2, campaign_id=20, candidate_id="cand_dup"),
        promoted_job(job_id=3, campaign_id=20, candidate_id="cand_new", symbol="NVDA"),
    ]
    status_conn = FakeImportConnection(promoted_jobs=rows, existing_champion_rows=[champion_row(existing)])
    import_conn = FakeImportConnection(promoted_jobs=rows, existing_champion_rows=[champion_row(existing)])

    reported = rci.research_champion_status(status_conn)["eligible_promoted_jobs"]
    result = rci.import_research_champions(import_conn, max_champions=5000)

    assert reported == result["imported"] == 1


def test_an_import_that_adds_nothing_says_why() -> None:
    """'0 champions added' with no reason is what made this look like a bug."""
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    conn = FakeImportConnection(
        promoted_jobs=[promoted_job(job_id=2, campaign_id=20, candidate_id="cand_dup")],
        existing_champion_rows=[champion_row(existing)],
    )

    result = rci.import_research_champions(conn, max_champions=25)

    assert result["imported"] == 0
    assert result["examined"] == 1
    assert result["skipped_duplicate_of_existing_champion"] == 1


def test_an_already_graduated_elite_also_covers_a_cluster() -> None:
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    conn = FakeImportConnection(
        promoted_jobs=[promoted_job(job_id=2, campaign_id=20, candidate_id="cand_dup")],
        existing_champion_rows=[champion_row(existing, state="elite")],
    )

    assert rci.research_champion_status(conn)["eligible_promoted_jobs"] == 0


def test_cluster_key_ignores_candidate_id_and_campaign_id() -> None:
    left = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    right = promoted_job(job_id=2, campaign_id=99, candidate_id="cand_totally_different")

    assert rci._cluster_key(left) == rci._cluster_key(right)


def test_cluster_key_distinguishes_genuinely_different_strategies() -> None:
    base = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    different_blocks = promoted_job(job_id=2, campaign_id=10, candidate_id="cand_b", blocks="rsi_60")
    different_symbol = promoted_job(job_id=3, campaign_id=10, candidate_id="cand_c", symbol="NVDA")
    different_timeframe = promoted_job(job_id=4, campaign_id=10, candidate_id="cand_d", timeframe="15m")

    assert rci._cluster_key(base) != rci._cluster_key(different_blocks)
    assert rci._cluster_key(base) != rci._cluster_key(different_symbol)
    assert rci._cluster_key(base) != rci._cluster_key(different_timeframe)


def test_cluster_key_separates_strategies_by_their_execution_parameters() -> None:
    base = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    tuned = promoted_job(job_id=2, campaign_id=10, candidate_id="cand_b")
    tuned["candidate"]["parameters"] = {**tuned["candidate"]["parameters"], "rsi_min": 62}

    assert rci._cluster_key(base) != rci._cluster_key(tuned)


def test_cluster_key_ignores_research_lineage_and_provenance() -> None:
    # Lineage is provenance, not identity: the same executable strategy reached
    # by two different mutation paths is still one strategy, and treating the
    # paths as distinct is what let the queue fill with copies.
    base = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    different_parent = promoted_job(job_id=2, campaign_id=20, candidate_id="cand_c", parent="root_b")
    # `candidate_execution_key` excludes research-provenance parameters, so a
    # differing hypothesis id must not split a cluster either.
    different_provenance = promoted_job(job_id=3, campaign_id=30, candidate_id="cand_d")
    different_provenance["candidate"]["parameters"] = {
        **different_provenance["candidate"]["parameters"],
        "hypothesis_version_id": 991,
        "source_campaign_id": 30,
    }

    assert rci._cluster_key(base) == rci._cluster_key(different_parent)
    assert rci._cluster_key(base) == rci._cluster_key(different_provenance)


def test_cluster_key_is_stable_against_dict_insertion_order() -> None:
    # The previous key hashed `str(dict)`, whose output depends on insertion
    # order, so the same blocks could hash two different ways.
    left = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    right = promoted_job(job_id=2, campaign_id=10, candidate_id="cand_b")
    left["candidate"]["blocks"] = {"entry": "momentum_rsi_55", "exit": "time_exit_12"}
    right["candidate"]["blocks"] = {"exit": "time_exit_12", "entry": "momentum_rsi_55"}

    assert rci._cluster_key(left) == rci._cluster_key(right)


def test_import_skips_a_job_that_duplicates_an_already_imported_champion() -> None:
    # Same effective strategy (symbol/timeframe/family/parent/blocks/direction)
    # as an existing research_champion, but under a fresh candidate_id from a
    # different campaign -- the exact shape repeated campaign runs produce.
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    duplicate = promoted_job(job_id=2, campaign_id=20, candidate_id="cand_new_but_same_strategy")
    conn = FakeImportConnection(
        promoted_jobs=[duplicate],
        existing_champion_rows=[
            {
                "candidate_id": "cand_original",
                "campaign_id": 10,
                "symbol": existing["symbol"],
                "timeframe": existing["timeframe"],
                "candidate": existing["candidate"],
                "strategy_family": existing["strategy_family"],
                "job_family_id": existing["family_id"],
                "job_parent_candidate_id": existing["parent_candidate_id"],
                "promotion_state": "research_champion",
            }
        ],
    )

    result = rci.import_research_champions(conn, max_champions=25)

    assert result["imported"] == 0
    assert conn.inserted == []
    assert result["already_covered_clusters"] == 1
    assert result["dedupe_clusters_seen"] == 0


def test_import_still_admits_a_genuinely_different_strategy() -> None:
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_original")
    different = promoted_job(job_id=2, campaign_id=20, candidate_id="cand_different", symbol="NVDA")
    conn = FakeImportConnection(
        promoted_jobs=[different],
        existing_champion_rows=[
            {
                "candidate_id": "cand_original",
                "campaign_id": 10,
                "symbol": existing["symbol"],
                "timeframe": existing["timeframe"],
                "candidate": existing["candidate"],
                "strategy_family": existing["strategy_family"],
                "job_family_id": existing["family_id"],
                "job_parent_candidate_id": existing["parent_candidate_id"],
                "promotion_state": "research_champion",
            }
        ],
    )

    result = rci.import_research_champions(conn, max_champions=25)

    assert result["imported"] == 1
    assert len(conn.inserted) == 1
    assert result["dedupe_clusters_seen"] == 1


def test_import_never_treats_a_demoted_rows_cluster_as_already_covered() -> None:
    # A demoted row (failed dedup or consistency) must not permanently block a
    # cluster from ever being re-imported -- only a *live* champion or elite does.
    existing = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_demoted")
    reimport_candidate = promoted_job(job_id=2, campaign_id=20, candidate_id="cand_new")
    conn = FakeImportConnection(
        promoted_jobs=[reimport_candidate],
        existing_champion_rows=[
            {
                "candidate_id": "cand_demoted",
                "campaign_id": 10,
                "symbol": existing["symbol"],
                "timeframe": existing["timeframe"],
                "candidate": existing["candidate"],
                "strategy_family": existing["strategy_family"],
                "job_family_id": existing["family_id"],
                "job_parent_candidate_id": existing["parent_candidate_id"],
                "promotion_state": "demoted",
            }
        ],
    )

    result = rci.import_research_champions(conn, max_champions=25)

    assert result["imported"] == 1
    assert result["already_covered_clusters"] == 0


def test_dedupe_keeps_the_highest_scoring_row_and_demotes_the_rest() -> None:
    base = promoted_job(job_id=1, campaign_id=1, candidate_id="cand_a")

    def champion_row(candidate_id, campaign_id, score):
        return {
            "id": campaign_id,
            "candidate_id": candidate_id,
            "campaign_id": campaign_id,
            "research_score": score,
            "symbol": base["symbol"],
            "timeframe": base["timeframe"],
            "candidate": base["candidate"],
            "strategy_family": base["strategy_family"],
            "job_family_id": base["family_id"],
            "job_parent_candidate_id": base["parent_candidate_id"],
        }

    rows = [champion_row("cand_low", 1, 0.4), champion_row("cand_best", 2, 0.9), champion_row("cand_mid", 3, 0.6)]
    conn = FakeImportConnection(champion_rows_for_dedupe=rows)

    result = rci.dedupe_research_champions(conn)

    assert result["champions_examined"] == 3
    assert result["clusters_examined"] == 1
    assert result["duplicate_clusters"] == 1
    assert result["champions_demoted"] == 2
    assert conn.commits == 1
    assert len(conn.demote_calls) == 1
    reason, demoted_ids = conn.demote_calls[0]
    assert "cand_best" in reason
    assert sorted(demoted_ids) == [1, 3]


def test_dedupe_leaves_a_unique_champion_untouched() -> None:
    base = promoted_job(job_id=1, campaign_id=1, candidate_id="cand_a")
    row = {
        "id": 1,
        "candidate_id": "cand_a",
        "campaign_id": 1,
        "research_score": 0.7,
        "symbol": base["symbol"],
        "timeframe": base["timeframe"],
        "candidate": base["candidate"],
        "strategy_family": base["strategy_family"],
        "job_family_id": base["family_id"],
        "job_parent_candidate_id": base["parent_candidate_id"],
    }
    conn = FakeImportConnection(champion_rows_for_dedupe=[row])

    result = rci.dedupe_research_champions(conn)

    assert result["duplicate_clusters"] == 0
    assert result["champions_demoted"] == 0
    assert conn.demote_calls == []


def test_dedupe_dry_run_reports_without_writing() -> None:
    base = promoted_job(job_id=1, campaign_id=1, candidate_id="cand_a")

    def champion_row(candidate_id, campaign_id, score):
        return {
            "id": campaign_id,
            "candidate_id": candidate_id,
            "campaign_id": campaign_id,
            "research_score": score,
            "symbol": base["symbol"],
            "timeframe": base["timeframe"],
            "candidate": base["candidate"],
            "strategy_family": base["strategy_family"],
            "job_family_id": base["family_id"],
            "job_parent_candidate_id": base["parent_candidate_id"],
        }

    rows = [champion_row("cand_low", 1, 0.4), champion_row("cand_best", 2, 0.9)]
    conn = FakeImportConnection(champion_rows_for_dedupe=rows)

    result = rci.dedupe_research_champions(conn, dry_run=True)

    assert result["dry_run"] is True
    assert result["champions_demoted"] == 1
    assert conn.demote_calls == []
    assert conn.commits == 0


def test_dedupe_never_touches_rows_outside_research_champion_state() -> None:
    # The dedupe query itself is scoped to promotion_state = 'research_champion'
    # only; an already-graduated elite is structurally unreachable here even if
    # it happens to share a cluster with something in the pool.
    conn = FakeImportConnection(champion_rows_for_dedupe=[])

    result = rci.dedupe_research_champions(conn)

    assert result["champions_examined"] == 0
    assert result["duplicate_clusters"] == 0
