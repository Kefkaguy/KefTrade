"""The cheap test that runs before a campaign.

Every series here is synthetic and built so the right answer is known before
the measurement runs: a signal that fires before real up-moves must score, a
coin-flip signal must not, and — the case that matters most — a signal with no
skill on a rising market must NOT be credited for the drift it merely sat in.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.signal_diagnostics import (
    MINIMUM_SIGNALS_FOR_A_VERDICT,
    MINIMUM_T_STATISTIC,
    _load_dataset_cached,
    claim_next_signal_diagnostics_job,
    enqueue_signal_diagnostics_job,
    measure_signal_edge,
    round_trip_cost_bps,
    run_claimed_signal_diagnostics_job,
    run_one_signal_diagnostics_job,
    summarize_edge,
    update_signal_diagnostics_job_progress,
)
from app.services.strategy import StrategyDecision

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def _rows(closes, *, opens=None):
    rows = []
    for index, close in enumerate(closes):
        open_price = opens[index] if opens is not None else (closes[index - 1] if index else close)
        timestamp = START + timedelta(minutes=30 * index)
        rows.append(
            {
                "candle": {
                    "symbol": "TEST",
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "open": Decimal(str(open_price)),
                    "high": Decimal(str(max(open_price, close))),
                    "low": Decimal(str(min(open_price, close))),
                    "close": Decimal(str(close)),
                    "volume": Decimal("1000"),
                },
                "feature": {"timestamp": timestamp},
            }
        )
    return rows


def _setup(direction="long"):
    close = Decimal("100")
    return StrategyDecision("setup", (close, close), None, None, None, ["test"], direction=direction)


def _avoid():
    return StrategyDecision("avoid", None, None, None, None, ["test"])


def _flat_with_jumps(count=600, period=7, jump=0.004):
    """Flat except for a jump every `period` bars, so a signal that fires one
    bar before each jump has genuine foresight."""
    closes = [100.0]
    for index in range(1, count):
        closes.append(closes[-1] * (1 + jump) if index % period == 0 else closes[-1])
    return closes


# ---------------------------------------------------------------------------
# Detecting a signal that is really there
# ---------------------------------------------------------------------------

def test_a_signal_that_fires_before_real_moves_is_detected():
    rows = _rows(_flat_with_jumps())

    # Entry fills at bar i+1's open, which equals bar i's close; the jump lands
    # on the bar after that. Firing at i == period-2 captures it.
    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 5 else _avoid()

    measurement = measure_signal_edge(rows, decide, {}, horizons=(1, 2, 4))
    summary = summarize_edge(measurement, cost_bps=1.0)

    assert measurement["signal_count"] >= MINIMUM_SIGNALS_FOR_A_VERDICT
    assert summary["excess_edge_bps"] > 0
    assert summary["t_statistic"] > MINIMUM_T_STATISTIC
    assert summary["verdict"] == "predictive"


def test_a_short_signal_before_real_drops_is_detected():
    closes = [100.0]
    for index in range(1, 600):
        closes.append(closes[-1] * (1 - 0.004) if index % 7 == 0 else closes[-1])
    rows = _rows(closes)

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup("short") if index % 7 == 5 else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2)), cost_bps=1.0)

    assert summary["excess_edge_bps"] > 0
    assert summary["verdict"] == "predictive"


# ---------------------------------------------------------------------------
# The case that matters most: drift is not skill
# ---------------------------------------------------------------------------

def test_a_skill_free_signal_on_a_rising_market_earns_no_credit():
    """A long-only signal with random timing on a steadily rising market shows
    a large POSITIVE raw return and zero real edge. Reporting the raw number
    would call every long-only family on a bull market predictive."""
    rows = _rows([100.0 * (1.0015**index) for index in range(600)])

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 5 == 0 else _avoid()

    measurement = measure_signal_edge(rows, decide, {}, horizons=(4,))
    summary = summarize_edge(measurement, cost_bps=1.0)
    horizon = measurement["by_horizon"][0]

    assert horizon["raw_edge_bps"] > 50, "the drift alone should look impressive"
    assert abs(horizon["excess_edge_bps"]) < 1e-6, "but the timing added nothing"
    assert summary["verdict"] == "no_signal"


def test_raw_edge_and_excess_differ_by_exactly_the_drift():
    rows = _rows([100.0 * (1.001**index) for index in range(400)])

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 4 == 0 else _avoid()

    horizon = measure_signal_edge(rows, decide, {}, horizons=(2,))["by_horizon"][0]

    assert horizon["raw_edge_bps"] - horizon["excess_edge_bps"] == pytest.approx(
        horizon["unconditional_drift_bps"], abs=1e-3
    )


# ---------------------------------------------------------------------------
# Rejecting signals that are not there
# ---------------------------------------------------------------------------

def test_a_coin_flip_signal_on_a_random_walk_is_rejected():
    import random

    rng = random.Random(7)
    price = 100.0
    closes = []
    for _ in range(1200):
        price *= 1 + rng.gauss(0, 0.002)
        closes.append(price)
    rows = _rows(closes)
    flips = random.Random(11)

    def decide(candle, feature, recent, params):
        return _setup() if flips.random() < 0.2 else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2, 4, 8)), cost_bps=1.0)

    assert summary["verdict"] == "no_signal"
    assert abs(summary["t_statistic"]) < MINIMUM_T_STATISTIC


def test_a_signal_that_rarely_fires_gets_no_verdict():
    """A spectacular mean over nine observations is not a measurement."""
    rows = _rows(_flat_with_jumps())

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index in (60, 67, 74) else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2)), cost_bps=1.0)

    assert summary["verdict"] == "insufficient_signals"
    assert summary["clears_cost"] is False


def test_a_signal_that_never_fires_is_handled():
    rows = _rows([100.0] * 300)
    summary = summarize_edge(
        measure_signal_edge(rows, lambda *args: _avoid(), {}, horizons=(1,)), cost_bps=1.0
    )

    assert summary["verdict"] == "insufficient_signals"


# ---------------------------------------------------------------------------
# The cost comparison is the decision
# ---------------------------------------------------------------------------

def test_a_real_signal_smaller_than_costs_is_named_as_such():
    """The distinction that decides what to do next: 'no signal' means retire,
    'signal below cost' means widen the stop or lengthen the hold."""
    rows = _rows(_flat_with_jumps(jump=0.0006))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 5 else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2)), cost_bps=30.0)

    assert summary["statistically_significant"] is True
    assert summary["clears_cost"] is False
    assert summary["verdict"] == "signal_below_cost"
    assert "not the problem" in summary["detail"]


def test_the_same_signal_clears_a_realistic_cost():
    rows = _rows(_flat_with_jumps(jump=0.0006))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 5 else _avoid()

    measurement = measure_signal_edge(rows, decide, {}, horizons=(1, 2))

    assert summarize_edge(measurement, cost_bps=30.0)["verdict"] == "signal_below_cost"
    assert summarize_edge(measurement, cost_bps=1.0)["verdict"] == "predictive"


def test_the_cost_comes_from_the_live_configuration():
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    expected = 2 * (float(BASE_V2_PARAMETERS["fee_rate"]) + float(BASE_V2_PARAMETERS["slippage_rate"])) * 10_000

    assert round_trip_cost_bps() == pytest.approx(expected)


# ---------------------------------------------------------------------------
# No lookahead
# ---------------------------------------------------------------------------

def test_a_signal_fired_on_the_bar_that_already_moved_captures_nothing():
    """The jump happens inside bar j, between its open and its close. A signal
    reading bar j can only fill at bar j+1's open — after the move — so it must
    earn nothing and score below the drift it missed. Filling at the signal
    bar's close instead would show a large fake edge here."""
    rows = _rows(_flat_with_jumps(period=7, jump=0.01))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 0 else _avoid()

    horizon = measure_signal_edge(rows, decide, {}, horizons=(1,))["by_horizon"][0]

    assert horizon["raw_edge_bps"] == pytest.approx(0.0, abs=1e-6)
    assert horizon["excess_edge_bps"] < 0


def test_a_signal_fired_one_bar_early_does_capture_the_move():
    """The mirror of the test above, so the two together pin the fill
    convention rather than just asserting a negative number."""
    rows = _rows(_flat_with_jumps(period=7, jump=0.01))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 6 else _avoid()

    horizon = measure_signal_edge(rows, decide, {}, horizons=(1,))["by_horizon"][0]

    assert horizon["raw_edge_bps"] > 90
    assert horizon["excess_edge_bps"] > 0


def test_the_best_horizon_is_chosen_by_significance_not_size():
    """The largest edge in a sweep is often the noisiest; picking it is how a
    horizon sweep becomes a selection bias."""
    measurement = {
        "signal_count": 500,
        "by_horizon": [
            {"horizon_bars": 2, "signals": 500, "raw_edge_bps": 5.0, "unconditional_drift_bps": 0.0,
             "excess_edge_bps": 5.0, "t_statistic": 6.0, "hit_rate": 0.6},
            {"horizon_bars": 32, "signals": 500, "raw_edge_bps": 40.0, "unconditional_drift_bps": 0.0,
             "excess_edge_bps": 40.0, "t_statistic": 1.2, "hit_rate": 0.52},
        ],
    }

    summary = summarize_edge(measurement, cost_bps=1.0)

    assert summary["best_horizon_bars"] == 2
    assert summary["excess_edge_bps"] == 5.0


def test_the_significance_bar_is_above_the_conventional_two():
    """Several horizons are tested and the best kept, so 2.0 would under-state
    the real false-positive rate."""
    assert MINIMUM_T_STATISTIC > 2.0


# ---------------------------------------------------------------------------
# N+1 fix: the same (symbol, timeframe, dataset_id) must be loaded once
# ---------------------------------------------------------------------------

class _FakeCandidate:
    def __init__(self, parameters):
        self.parameters = parameters


def test_the_same_symbol_is_loaded_once_across_repeated_calls(monkeypatch):
    """This is the exact bug: without a shared cache, every (family, variant)
    pair re-issues the candle query for a symbol whose result never changes
    within one sweep."""
    calls = []

    def fake_load_intraday_backtest_dataset(conn, symbol, timeframe, *, dataset_id):
        calls.append((symbol, timeframe, dataset_id))
        return {"rows": [], "candles": [], "features": []}

    monkeypatch.setattr(
        "app.services.labs.intraday.dataset.load_intraday_backtest_dataset",
        fake_load_intraday_backtest_dataset,
    )

    cache: dict = {}
    candidate = _FakeCandidate({"strategy_architecture": "session_momentum_v2"})
    for _ in range(12):  # simulating 12 families asking for the same symbol
        _load_dataset_cached(None, candidate, "NVDA", "30m", 7, cache)

    assert len(calls) == 1


def test_different_symbols_still_each_load_once(monkeypatch):
    calls = []

    def fake_load_intraday_backtest_dataset(conn, symbol, timeframe, *, dataset_id):
        calls.append(symbol)
        return {"rows": [], "candles": [], "features": []}

    monkeypatch.setattr(
        "app.services.labs.intraday.dataset.load_intraday_backtest_dataset",
        fake_load_intraday_backtest_dataset,
    )

    cache: dict = {}
    candidate = _FakeCandidate({"strategy_architecture": "session_momentum_v2"})
    for symbol in ("NVDA", "TSLA", "NVDA", "TSLA", "AMD"):
        _load_dataset_cached(None, candidate, symbol, "30m", 7, cache)

    assert sorted(calls) == ["AMD", "NVDA", "TSLA"]


class _FakeJobsConn:
    """Minimal in-memory stand-in for the jobs table, just enough to exercise
    enqueue / claim / complete without a real database."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self._next_id = 1
        self.commits = 0

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        params = params or ()

        if text.startswith("CREATE TABLE") or text.startswith("CREATE INDEX") or text.startswith("ALTER TABLE"):
            return _FakeJobsResult(None)

        if text.startswith("INSERT INTO research_signal_diagnostics_jobs"):
            timeframe, dataset_id, architectures, max_variants, max_symbols = params
            row_id = self._next_id
            self._next_id += 1
            row = {
                "id": row_id,
                "timeframe": timeframe,
                "dataset_id": dataset_id,
                "architectures": architectures.obj if architectures is not None else None,
                "max_variants": max_variants,
                "max_symbols": max_symbols,
                "status": "queued",
                "result": None,
                "error": None,
                "progress_total": 0,
                "progress_completed": 0,
                "progress_current": None,
            }
            self.rows[row_id] = row
            return _FakeJobsResult(dict(row))

        if "SET status = 'running'" in text:
            queued = sorted((row for row in self.rows.values() if row["status"] == "queued"), key=lambda r: r["id"])
            if not queued:
                return _FakeJobsResult(None)
            queued[0]["status"] = "running"
            return _FakeJobsResult(dict(queued[0]))

        if "SET status = 'completed'" in text:
            result, job_id = params
            row = self.rows[job_id]
            row["status"] = "completed"
            row["result"] = result.obj if hasattr(result, "obj") else result
            row["progress_completed"] = max(row["progress_completed"], row["progress_total"])
            row["progress_current"] = None
            return _FakeJobsResult(dict(row))

        if "SET status = 'failed'" in text:
            error, job_id = params
            row = self.rows[job_id]
            row["status"] = "failed"
            row["error"] = error
            row["progress_current"] = None
            return _FakeJobsResult(dict(row))

        if "SET progress_total" in text:
            total, completed, current, job_id = params
            row = self.rows[job_id]
            row["progress_total"] = total
            row["progress_completed"] = completed
            row["progress_current"] = current
            return _FakeJobsResult(dict(row))

        if text.startswith("SELECT * FROM research_signal_diagnostics_jobs WHERE id"):
            (job_id,) = params
            row = self.rows.get(job_id)
            return _FakeJobsResult(dict(row) if row else None)

        raise AssertionError(f"unexpected query: {text[:80]}")

    def commit(self):
        self.commits += 1


class _FakeJobsResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


# ---------------------------------------------------------------------------
# Background job queue: the fix for the 502s
# ---------------------------------------------------------------------------

def test_enqueue_returns_immediately_without_running_anything(monkeypatch):
    """The whole point: enqueuing must never touch measure_signal_edge."""
    called = []
    monkeypatch.setattr(
        "app.services.signal_diagnostics.run_signal_diagnostics",
        lambda *a, **k: called.append(1),
    )

    job = enqueue_signal_diagnostics_job(_FakeJobsConn(), timeframe="30m")

    assert job["status"] == "queued"
    assert called == []


def test_claim_picks_the_oldest_queued_job_and_marks_it_running():
    conn = _FakeJobsConn()
    first = enqueue_signal_diagnostics_job(conn, timeframe="30m")
    enqueue_signal_diagnostics_job(conn, timeframe="15m")

    claimed = claim_next_signal_diagnostics_job(conn)

    assert claimed["id"] == first["id"]
    assert claimed["status"] == "running"


def test_claim_on_an_empty_queue_returns_none():
    assert claim_next_signal_diagnostics_job(_FakeJobsConn()) is None


def test_a_successful_job_stores_its_result(monkeypatch):
    fake_result = {"families_measured": 3, "predictive_families": ["session_momentum_v2"]}
    monkeypatch.setattr(
        "app.services.signal_diagnostics.run_signal_diagnostics",
        lambda conn, **kwargs: fake_result,
    )
    conn = _FakeJobsConn()
    enqueue_signal_diagnostics_job(conn, timeframe="30m")
    job = claim_next_signal_diagnostics_job(conn)

    completed = run_claimed_signal_diagnostics_job(conn, job)

    assert completed["status"] == "completed"
    assert completed["result"] == fake_result


def test_job_progress_is_persisted_independently():
    conn = _FakeJobsConn()
    job = enqueue_signal_diagnostics_job(conn, timeframe="15m")

    update_signal_diagnostics_job_progress(
        conn,
        job["id"],
        total=7,
        completed=3,
        current="VWAP Bounce",
    )
    stored = conn.rows[job["id"]]

    assert stored["progress_total"] == 7
    assert stored["progress_completed"] == 3
    assert stored["progress_current"] == "VWAP Bounce"


def test_a_failing_job_is_recorded_as_failed_not_raised(monkeypatch):
    """One bad dataset must not crash the worker loop -- the same principle
    run_signal_diagnostics already applies per-family, one level up."""

    def boom(conn, **kwargs):
        raise ValueError("no intraday dataset snapshot exists to measure against")

    monkeypatch.setattr("app.services.signal_diagnostics.run_signal_diagnostics", boom)
    conn = _FakeJobsConn()
    enqueue_signal_diagnostics_job(conn, timeframe="30m")
    job = claim_next_signal_diagnostics_job(conn)

    completed = run_claimed_signal_diagnostics_job(conn, job)

    assert completed["status"] == "failed"
    assert "no intraday dataset snapshot" in completed["error"]


def test_run_one_job_processes_a_single_queued_item(monkeypatch):
    monkeypatch.setattr(
        "app.services.signal_diagnostics.run_signal_diagnostics",
        lambda conn, **kwargs: {"families_measured": 0},
    )
    conn = _FakeJobsConn()
    enqueue_signal_diagnostics_job(conn, timeframe="30m")

    result = run_one_signal_diagnostics_job(conn)

    assert result["status"] == "completed"
    assert run_one_signal_diagnostics_job(conn) is None, "the queue should be empty now"


class _QueueHealthConn:
    """Stands in for the aggregate query behind signal_diagnostics_queue_health."""

    def __init__(self, *, queued=0, running=0, oldest_queued_seconds=None, last_started_at=None):
        self.row = {
            "queued": queued,
            "running": running,
            "oldest_queued_seconds": oldest_queued_seconds,
            "last_started_at": last_started_at,
        }

    def execute(self, query, params=None):
        return _FakeJobsResult(dict(self.row))

    def commit(self):
        pass


def test_a_long_unclaimed_job_reports_the_worker_as_stopped():
    """The failure the user hit: job queued, nothing running, ten minutes of
    polling, and a timeout that reads like a hung measurement instead of a
    missing process."""
    from app.services.signal_diagnostics import signal_diagnostics_queue_health

    health = signal_diagnostics_queue_health(
        _QueueHealthConn(queued=1, running=0, oldest_queued_seconds=300)
    )

    assert health["worker_appears_stopped"] is True
    assert "signal_diagnostics_runner" in health["detail"]


def test_a_briefly_queued_job_is_not_called_stopped():
    """A worker busy with a previous job, on its poll interval, must not be
    misreported as absent."""
    from app.services.signal_diagnostics import signal_diagnostics_queue_health

    health = signal_diagnostics_queue_health(
        _QueueHealthConn(queued=1, running=0, oldest_queued_seconds=5)
    )

    assert health["worker_appears_stopped"] is False


def test_a_running_job_means_the_worker_is_alive():
    from app.services.signal_diagnostics import signal_diagnostics_queue_health

    health = signal_diagnostics_queue_health(
        _QueueHealthConn(queued=2, running=1, oldest_queued_seconds=600)
    )

    assert health["worker_appears_stopped"] is False


def test_an_empty_queue_is_not_stalled():
    from app.services.signal_diagnostics import signal_diagnostics_queue_health

    health = signal_diagnostics_queue_health(_QueueHealthConn())

    assert health["worker_appears_stopped"] is False
    assert health["queued"] == 0


class _DDLRecordingConn(_FakeJobsConn):
    """Records whether any statement issued DDL."""

    def __init__(self):
        super().__init__()
        self.ddl_statements: list[str] = []

    def execute(self, query, params=None):
        text = " ".join(str(query).split()).upper()
        if any(token in text for token in ("CREATE TABLE", "CREATE INDEX", "ALTER TABLE", "DROP TABLE")):
            self.ddl_statements.append(text[:60])
        return super().execute(query, params)


def test_polling_a_job_status_never_issues_ddl():
    """The 502 bug. `CREATE TABLE IF NOT EXISTS` takes an ACCESS EXCLUSIVE
    lock even when the table exists, so calling it on an endpoint the UI polls
    every two seconds serialised every poll behind every other transaction on
    that table. Schema belongs to migrations and to startup, never to a
    request path."""
    from app.services.signal_diagnostics import get_signal_diagnostics_job

    conn = _DDLRecordingConn()
    enqueue_signal_diagnostics_job(conn, timeframe="30m")
    conn.ddl_statements.clear()

    get_signal_diagnostics_job(conn, 1)

    assert conn.ddl_statements == []


def test_enqueuing_never_issues_ddl():
    conn = _DDLRecordingConn()

    enqueue_signal_diagnostics_job(conn, timeframe="30m")

    assert conn.ddl_statements == []


def test_claiming_a_job_never_issues_ddl():
    conn = _DDLRecordingConn()
    enqueue_signal_diagnostics_job(conn, timeframe="30m")
    conn.ddl_statements.clear()

    claim_next_signal_diagnostics_job(conn)

    assert conn.ddl_statements == []


def test_every_write_commits_so_no_transaction_is_held_open():
    """The other half of the original bug: results must be visible to a
    poller immediately, not held inside one long-lived transaction."""
    conn = _FakeJobsConn()
    enqueue_signal_diagnostics_job(conn, timeframe="30m")

    assert conn.commits >= 1


def test_cross_sectional_cache_key_includes_lookback_bars(monkeypatch):
    """A cross-sectional dataset's shape depends on lookback_bars too, so two
    different lookbacks must not collide in the cache."""
    calls = []

    def fake_load_cross_sectional_intraday_dataset(conn, symbol, timeframe, *, dataset_id, lookback_bars):
        calls.append(lookback_bars)
        return {"rows": [], "candles": [], "features": []}

    monkeypatch.setattr(
        "app.services.labs.intraday.cross_sectional_dataset.load_cross_sectional_intraday_dataset",
        fake_load_cross_sectional_intraday_dataset,
    )

    cache: dict = {}
    short = _FakeCandidate({"strategy_architecture": "cross_sectional_momentum_v2", "cross_sectional_lookback_bars": 4})
    long = _FakeCandidate({"strategy_architecture": "cross_sectional_momentum_v2", "cross_sectional_lookback_bars": 16})

    _load_dataset_cached(None, short, "NVDA", "30m", 7, cache)
    _load_dataset_cached(None, short, "NVDA", "30m", 7, cache)
    _load_dataset_cached(None, long, "NVDA", "30m", 7, cache)

    assert sorted(calls) == [4, 16]
