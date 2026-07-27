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


def test_cluster_key_ignores_candidate_id_and_campaign_id() -> None:
    left = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    right = promoted_job(job_id=2, campaign_id=99, candidate_id="cand_totally_different")

    assert rci._cluster_key(left) == rci._cluster_key(right)


def test_cluster_key_distinguishes_different_blocks_or_lineage() -> None:
    base = promoted_job(job_id=1, campaign_id=10, candidate_id="cand_a")
    different_blocks = promoted_job(job_id=2, campaign_id=10, candidate_id="cand_b", blocks="rsi_60")
    different_parent = promoted_job(job_id=3, campaign_id=10, candidate_id="cand_c", parent="root_b")

    assert rci._cluster_key(base) != rci._cluster_key(different_blocks)
    assert rci._cluster_key(base) != rci._cluster_key(different_parent)


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
