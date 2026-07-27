"""Pooled cross-sectional evidence: the elite gate applied to trades pooled
across every symbol a canonical candidate was tested on, instead of siloed
per-symbol evaluation. See app/services/labs/intraday/pooled_evidence.py.
"""

from decimal import Decimal

import pytest

from app.services.labs.intraday.pooled_evidence import (
    HOLDOUT_CONFIRMATION_VERSION,
    MINIMUM_CONTRIBUTING_SYMBOLS,
    POOLED_EVIDENCE_VERSION,
    compute_holdout_confirmation,
    compute_pooled_candidate_evidence,
)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakePooledConn:
    def __init__(self, jobs, trades_by_job_id):
        self.jobs = jobs
        self.trades_by_job_id = trades_by_job_id
        self.inserted: dict[str, dict] = {}
        self.commits = 0

    def execute(self, query, params=None):
        params = params or ()
        stripped = query.strip()
        if stripped.startswith("SELECT id, symbol, dataset_id"):
            return FakeResult(self.jobs)
        if stripped.startswith("SELECT symbol, net_pnl, pnl_pct, holding_period_hours"):
            job_ids = params[0]
            rows = [row for job_id in job_ids for row in self.trades_by_job_id.get(job_id, [])]
            return FakeResult(rows)
        if stripped.startswith("INSERT INTO research_candidate_stage_evidence"):
            (
                evidence_key, campaign_id, candidate_id, candidate_level, scope_type, scope_ref,
                gate_results, metrics, evidence_refs, promoted, calculation_version,
            ) = params
            row = {
                "evidence_key": evidence_key,
                "campaign_id": campaign_id,
                "candidate_id": candidate_id,
                "candidate_level": candidate_level,
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "gate_results": gate_results.obj,
                "metrics": metrics.obj,
                "evidence_refs": evidence_refs.obj,
                "promoted": promoted,
                "calculation_version": calculation_version,
            }
            self.inserted[evidence_key] = row
            return FakeResult([row])
        raise AssertionError(f"unexpected query: {stripped[:80]}")

    def commit(self):
        self.commits += 1


def job(job_id, symbol, candidate_id, *, max_drawdown=0.05, walk_forward=True, architecture="vwap_bounce_v2"):
    return {
        "id": job_id,
        "symbol": symbol,
        "dataset_id": 1,
        "candidate_id": candidate_id,
        "strategy_architecture": architecture,
        "job_max_drawdown": max_drawdown,
        "walk_forward_enabled": walk_forward,
    }


def trade(net_pnl, pnl_pct=0.01, holding_period_hours=2.0):
    return {"symbol": "X", "net_pnl": Decimal(str(net_pnl)), "pnl_pct": pnl_pct, "holding_period_hours": holding_period_hours}


# ---------------------------------------------------------------------------
# Core pooling behavior
# ---------------------------------------------------------------------------

def test_a_single_symbol_candidate_is_skipped_not_pooled():
    """Pooling one symbol with itself adds no real breadth -- see the
    module's MINIMUM_CONTRIBUTING_SYMBOLS constant."""
    jobs = [job(1, "NVDA", "candA")]
    conn = FakePooledConn(jobs, {1: [trade(10) for _ in range(20)]})

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert results == []
    assert MINIMUM_CONTRIBUTING_SYMBOLS == 2


def test_different_candidates_are_pooled_separately_not_mixed():
    jobs = [
        job(1, "NVDA", "candA"),
        job(2, "TSLA", "candA"),
        job(3, "NVDA", "candB"),
        job(4, "TSLA", "candB"),
    ]
    trades_by_job = {
        1: [trade(10) for _ in range(10)],
        2: [trade(10) for _ in range(10)],
        3: [trade(-10) for _ in range(10)],
        4: [trade(-10) for _ in range(10)],
    }
    conn = FakePooledConn(jobs, trades_by_job)

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    by_candidate = {r["candidate_id"]: r for r in results}
    assert set(by_candidate) == {"candA", "candB"}
    assert by_candidate["candA"]["metrics"]["number_of_trades"] == 20
    assert by_candidate["candB"]["metrics"]["number_of_trades"] == 20
    # candA is all wins, candB is all losses -- pooling must not blend them.
    assert by_candidate["candA"]["metrics"]["profit_factor_is_infinite"] is True
    assert by_candidate["candB"]["metrics"]["gross_profit"] == 0.0


def test_pooled_trade_count_sums_across_contributing_symbols():
    jobs = [job(1, "NVDA", "candA"), job(2, "TSLA", "candA"), job(3, "GOOGL", "candA")]
    trades_by_job = {1: [trade(5) for _ in range(12)], 2: [trade(5) for _ in range(10)], 3: [trade(5) for _ in range(8)]}
    conn = FakePooledConn(jobs, trades_by_job)

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert results[0]["metrics"]["number_of_trades"] == 30
    assert results[0]["metrics"]["contributing_symbol_count"] == 3
    assert results[0]["scope_ref"] == "GOOGL,NVDA,TSLA"


def test_profit_factor_and_expectancy_are_computed_from_the_pooled_trade_set():
    jobs = [job(1, "NVDA", "candA"), job(2, "TSLA", "candA")]
    # NVDA: 3 wins of 100, 1 loss of 50 -> gross_profit 300, gross_loss 50
    # TSLA: 2 wins of 100, 1 loss of 50 -> gross_profit 200, gross_loss 50
    # pooled: gross_profit 500, gross_loss 100 -> PF 5.0
    trades_by_job = {
        1: [trade(100), trade(100), trade(100), trade(-50)],
        2: [trade(100), trade(100), trade(-50)],
    }
    conn = FakePooledConn(jobs, trades_by_job)

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    metrics = results[0]["metrics"]
    assert metrics["gross_profit"] == pytest.approx(500.0)
    assert metrics["gross_loss"] == pytest.approx(100.0)
    assert metrics["profit_factor"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Drawdown honesty
# ---------------------------------------------------------------------------

def test_pooled_drawdown_is_the_worst_contributing_symbol_not_a_fabricated_blend():
    jobs = [
        job(1, "NVDA", "candA", max_drawdown=0.03),
        job(2, "TSLA", "candA", max_drawdown=0.18),
    ]
    trades_by_job = {1: [trade(10) for _ in range(10)], 2: [trade(10) for _ in range(10)]}
    conn = FakePooledConn(jobs, trades_by_job)

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert results[0]["metrics"]["max_drawdown"] == pytest.approx(0.18)
    assert results[0]["metrics"]["max_drawdown_source"] == "worst_contributing_symbol"


def test_walk_forward_enabled_requires_every_contributing_job_to_have_it():
    jobs = [
        job(1, "NVDA", "candA", walk_forward=True),
        job(2, "TSLA", "candA", walk_forward=False),
    ]
    trades_by_job = {1: [trade(10) for _ in range(10)], 2: [trade(10) for _ in range(10)]}
    conn = FakePooledConn(jobs, trades_by_job)

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert results[0]["metrics"]["walk_forward"]["enabled"] is False


# ---------------------------------------------------------------------------
# The actual point: pooling can promote what per-symbol gating never could
# ---------------------------------------------------------------------------

def test_pooling_can_clear_the_trade_count_gate_that_no_single_symbol_reached_alone():
    """Three symbols each individually fail the 30-trade minimum (10 trades
    each), but the SAME gate applied to their pooled evidence has 30 trades
    of a real, profitable pattern -- this is the entire point of the
    module: real breadth the per-symbol gate structurally cannot see."""
    jobs = [job(1, "NVDA", "candA"), job(2, "TSLA", "candA"), job(3, "GOOGL", "candA")]
    # Each symbol alone: 10 trades, PF well above 1.25, positive expectancy.
    per_symbol_trades = [trade(120) for _ in range(7)] + [trade(-50) for _ in range(3)]
    trades_by_job = {1: list(per_symbol_trades), 2: list(per_symbol_trades), 3: list(per_symbol_trades)}
    conn = FakePooledConn(jobs, trades_by_job)

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    metrics = results[0]["metrics"]
    assert metrics["number_of_trades"] == 30
    assert metrics["profit_factor"] > 1.25
    assert metrics["expectancy_per_trade"] > 0
    assert results[0]["promoted"] is True
    assert results[0]["gate_results"]["paper_ready"] is True


def test_pooling_does_not_promote_a_pooled_candidate_that_still_fails_the_gate():
    jobs = [job(1, "NVDA", "candA"), job(2, "TSLA", "candA")]
    weak_trades = [trade(10) for _ in range(5)] + [trade(-40) for _ in range(15)]
    conn = FakePooledConn(jobs, {1: list(weak_trades), 2: list(weak_trades)})

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert results[0]["promoted"] is False
    assert results[0]["gate_results"]["paper_ready"] is False
    assert len(results[0]["gate_results"]["failure_reasons"]) > 0


# ---------------------------------------------------------------------------
# Idempotency and identity
# ---------------------------------------------------------------------------

def test_evidence_key_is_stable_and_rerunning_updates_in_place_not_duplicates():
    jobs = [job(1, "NVDA", "candA"), job(2, "TSLA", "candA")]
    conn = FakePooledConn(jobs, {1: [trade(10) for _ in range(10)], 2: [trade(10) for _ in range(10)]})

    first = compute_pooled_candidate_evidence(conn, campaign_id=59)
    second = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert first[0]["evidence_key"] == second[0]["evidence_key"]
    assert first[0]["evidence_key"] == f"{POOLED_EVIDENCE_VERSION}:59:candA"
    assert len(conn.inserted) == 1


def test_candidate_level_and_scope_type_match_the_unused_staging_schema():
    jobs = [job(1, "NVDA", "candA"), job(2, "TSLA", "candA")]
    conn = FakePooledConn(jobs, {1: [trade(10) for _ in range(10)], 2: [trade(10) for _ in range(10)]})

    results = compute_pooled_candidate_evidence(conn, campaign_id=59)

    assert results[0]["candidate_level"] == "cluster_candidate"
    assert results[0]["scope_type"] == "cluster"


# ---------------------------------------------------------------------------
# Holdout confirmation: the same gate, on the untouched validation split only
# ---------------------------------------------------------------------------

class FakeHoldoutConn:
    def __init__(self, pooled_rows, jobs, holdout_trades_by_job_id):
        self.pooled_rows = pooled_rows
        self.jobs = jobs
        self.holdout_trades_by_job_id = holdout_trades_by_job_id
        self.inserted: dict[str, dict] = {}
        self.commits = 0

    def execute(self, query, params=None):
        params = params or ()
        stripped = query.strip()
        if stripped.startswith("SELECT candidate_id, promoted"):
            return FakeResult(self.pooled_rows)
        if stripped.startswith("SELECT id, symbol, candidate->>'candidate_id'"):
            return FakeResult(self.jobs)
        if stripped.startswith("SELECT job_id, symbol, net_pnl"):
            assert "dataset_split = 'validation'" in query, "holdout must read only the validation split"
            job_ids = params[0]
            return FakeResult([row for job_id in job_ids for row in self.holdout_trades_by_job_id.get(job_id, [])])
        if stripped.startswith("INSERT INTO research_candidate_stage_evidence"):
            (
                evidence_key, campaign_id, candidate_id, candidate_level, scope_type, scope_ref,
                gate_results, metrics, evidence_refs, promoted, calculation_version,
            ) = params
            row = {
                "evidence_key": evidence_key,
                "campaign_id": campaign_id,
                "candidate_id": candidate_id,
                "candidate_level": candidate_level,
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "gate_results": gate_results.obj,
                "metrics": metrics.obj,
                "evidence_refs": evidence_refs.obj,
                "promoted": promoted,
                "calculation_version": calculation_version,
            }
            self.inserted[evidence_key] = row
            return FakeResult([row])
        raise AssertionError(f"unexpected query: {stripped[:80]}")

    def commit(self):
        self.commits += 1


def holdout_job(job_id, symbol, candidate_id, *, max_drawdown=0.05, architecture="vwap_bounce_v2"):
    return {
        "id": job_id,
        "symbol": symbol,
        "candidate_id": candidate_id,
        "strategy_architecture": architecture,
        "job_max_drawdown": max_drawdown,
        "walk_forward_enabled": True,
    }


def holdout_trade(job_id, symbol, net_pnl, pnl_pct=0.01, holding_period_hours=2.0):
    return {
        "job_id": job_id,
        "symbol": symbol,
        "net_pnl": Decimal(str(net_pnl)),
        "pnl_pct": pnl_pct,
        "holding_period_hours": holding_period_hours,
    }


def strong_holdout(job_id, symbol, count=12):
    """A clearly profitable holdout sample for this job/symbol."""
    return [holdout_trade(job_id, symbol, 120) for _ in range(count - 3)] + [
        holdout_trade(job_id, symbol, -50) for _ in range(3)
    ]


def test_a_candidate_with_no_holdout_trades_is_never_confirmed():
    """Absence of an out-of-sample sample must never be read as passing one.
    This is the single most important property of the whole check."""
    pooled = [{"candidate_id": "candA", "promoted": True}]
    jobs = [holdout_job(1, "NVDA", "candA"), holdout_job(2, "TSLA", "candA")]
    conn = FakeHoldoutConn(pooled, jobs, {})

    results = compute_holdout_confirmation(conn, campaign_id=101)

    assert results[0]["confirmed"] is False
    assert results[0]["unconfirmed_reason"] == "NO_HOLDOUT_TRADES"
    assert conn.inserted == {}


def test_holdout_confirmation_requires_breadth_in_the_holdout_itself():
    """A confirmation resting on a single symbol is not cross-sectional, even
    when the pooled evidence behind it spanned several symbols."""
    pooled = [{"candidate_id": "candA", "promoted": True}]
    jobs = [holdout_job(1, "NVDA", "candA"), holdout_job(2, "TSLA", "candA")]
    conn = FakeHoldoutConn(pooled, jobs, {1: strong_holdout(1, "NVDA", 30)})

    results = compute_holdout_confirmation(conn, campaign_id=101)

    assert results[0]["confirmed"] is False
    assert results[0]["unconfirmed_reason"] == "INSUFFICIENT_HOLDOUT_SYMBOLS"
    assert conn.inserted == {}


def test_a_candidate_that_fails_the_gate_out_of_sample_is_not_confirmed():
    pooled = [{"candidate_id": "candA", "promoted": True}]
    jobs = [holdout_job(1, "NVDA", "candA"), holdout_job(2, "TSLA", "candA")]
    losing = {
        1: [holdout_trade(1, "NVDA", -40) for _ in range(12)],
        2: [holdout_trade(2, "TSLA", -40) for _ in range(12)],
    }
    conn = FakeHoldoutConn(pooled, jobs, losing)

    results = compute_holdout_confirmation(conn, campaign_id=101)

    assert results[0]["confirmed"] is False
    assert results[0]["unconfirmed_reason"] == "HOLDOUT_GATE_FAILED"
    assert results[0]["holdout_status"] == "rejected"
    assert conn.inserted == {}


def test_holdout_cannot_confirm_a_candidate_the_pooled_gate_already_rejected():
    """Strictly additive: this stage can only ever remove candidates."""
    pooled = [{"candidate_id": "candA", "promoted": False}]
    jobs = [holdout_job(1, "NVDA", "candA"), holdout_job(2, "TSLA", "candA")]
    conn = FakeHoldoutConn(pooled, jobs, {1: strong_holdout(1, "NVDA"), 2: strong_holdout(2, "TSLA")})

    results = compute_holdout_confirmation(conn, campaign_id=101)

    assert results[0]["holdout_status"] == "promoted"
    assert results[0]["confirmed"] is False
    assert results[0]["unconfirmed_reason"] == "POOLED_GATE_NOT_PROMOTED"
    assert conn.inserted == {}


def test_a_candidate_confirmed_out_of_sample_reaches_cluster_elite():
    pooled = [{"candidate_id": "candA", "promoted": True}]
    jobs = [holdout_job(1, "NVDA", "candA"), holdout_job(2, "TSLA", "candA")]
    conn = FakeHoldoutConn(pooled, jobs, {1: strong_holdout(1, "NVDA"), 2: strong_holdout(2, "TSLA")})

    results = compute_holdout_confirmation(conn, campaign_id=101)

    assert results[0]["confirmed"] is True
    assert results[0]["holdout_symbols"] == ["NVDA", "TSLA"]
    evidence = results[0]["evidence"]
    assert evidence["candidate_level"] == "cluster_elite"
    assert evidence["scope_type"] == "cluster"
    assert evidence["promoted"] is True
    assert evidence["calculation_version"] == HOLDOUT_CONFIRMATION_VERSION
    assert evidence["evidence_key"] == f"{HOLDOUT_CONFIRMATION_VERSION}:101:candA"
    assert evidence["metrics"]["dataset_split"] == "validation"
    assert evidence["evidence_refs"]["confirmation_basis"] == "validation_split_only"


def test_holdout_metrics_are_computed_only_from_contributing_holdout_jobs():
    """A job with no holdout trades must not lend its drawdown to the
    confirmation -- only jobs that actually contributed count."""
    pooled = [{"candidate_id": "candA", "promoted": True}]
    jobs = [
        holdout_job(1, "NVDA", "candA", max_drawdown=0.04),
        holdout_job(2, "TSLA", "candA", max_drawdown=0.06),
        holdout_job(3, "GOOGL", "candA", max_drawdown=0.30),
    ]
    conn = FakeHoldoutConn(pooled, jobs, {1: strong_holdout(1, "NVDA"), 2: strong_holdout(2, "TSLA")})

    results = compute_holdout_confirmation(conn, campaign_id=101)

    metrics = results[0]["evidence"]["metrics"]
    assert metrics["max_drawdown"] == pytest.approx(0.06)
    assert metrics["contributing_symbol_count"] == 2
    assert metrics["number_of_trades"] == 24
    assert sorted(results[0]["evidence"]["evidence_refs"]["job_ids"]) == [1, 2]


def test_holdout_confirmation_is_idempotent_by_evidence_key():
    pooled = [{"candidate_id": "candA", "promoted": True}]
    jobs = [holdout_job(1, "NVDA", "candA"), holdout_job(2, "TSLA", "candA")]
    conn = FakeHoldoutConn(pooled, jobs, {1: strong_holdout(1, "NVDA"), 2: strong_holdout(2, "TSLA")})

    first = compute_holdout_confirmation(conn, campaign_id=101)
    second = compute_holdout_confirmation(conn, campaign_id=101)

    assert first[0]["evidence"]["evidence_key"] == second[0]["evidence"]["evidence_key"]
    assert len(conn.inserted) == 1


def test_no_pooled_evidence_means_nothing_to_confirm():
    conn = FakeHoldoutConn([], [], {})

    assert compute_holdout_confirmation(conn, campaign_id=101) == []
