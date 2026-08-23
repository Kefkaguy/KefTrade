from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from app.services.established_paper_execution import (
    _mom_signal_is_missed,
    load_mom_shadow_signal,
)
from app.services.established_paper_signals import (
    CONNORS_ENTRY_THRESHOLD,
    RSI5_ENTRY_THRESHOLD,
    completed_daily_bars,
    evaluate_spy_connors,
    evaluate_spy_rsi5,
    rank_mom_12_1,
    wilder_rsi,
)


def bars(closes):
    start = datetime(2025, 1, 2, tzinfo=UTC)
    return [
        {
            "timestamp": start + timedelta(days=index),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def test_wilder_rsi_matches_the_recovered_artifact_formula():
    values = pd.Series([100, 101, 99, 98, 100, 97, 96, 99, 101, 100], dtype=float)
    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / 5, adjust=False, min_periods=5).mean()
    al = loss.ewm(alpha=1 / 5, adjust=False, min_periods=5).mean()
    expected = 100 - 100 / (1 + ag / al)
    assert wilder_rsi(values, 5).dropna().tolist() == pytest.approx(expected.dropna().tolist())


def test_daily_strategy_never_uses_todays_forming_bar():
    today = {"timestamp": datetime(2026, 8, 24, 4, tzinfo=UTC), "close": 100}
    prior = {"timestamp": datetime(2026, 8, 21, 4, tzinfo=UTC), "close": 99}
    before_close = datetime(2026, 8, 24, 19, tzinfo=UTC)  # 15:00 New York
    after_final = datetime(2026, 8, 24, 20, 16, tzinfo=UTC)  # 16:16 New York
    assert completed_daily_bars([prior, today], now=before_close) == [prior]
    assert completed_daily_bars([prior, today], now=after_final) == [prior, today]


def test_rsi5_enters_only_above_sma200_and_below_frozen_threshold():
    history = [100 + index * 0.1 for index in range(200)] + [130, 126, 122, 118, 115]
    decision = evaluate_spy_rsi5(bars(history), is_long=False)
    assert decision.indicators["rsi5"] < RSI5_ENTRY_THRESHOLD
    assert decision.close > decision.indicators["sma200"]
    assert decision.action == "enter_next_open"


def test_rsi5_exit_is_a_next_open_decision():
    history = [100 + index * 0.1 for index in range(200)] + [110, 112, 114, 116, 118]
    assert evaluate_spy_rsi5(bars(history), is_long=True).action == "exit_next_open"


def test_connors_entry_and_sma5_exit_are_separate_states():
    history = [100 + index * 0.1 for index in range(200)] + [130, 126, 122, 118, 115]
    entry = evaluate_spy_connors(bars(history), is_long=False)
    assert entry.indicators["connors_rsi"] < CONNORS_ENTRY_THRESHOLD
    assert entry.action == "enter_next_open"

    recovery = history + [125]
    exit_decision = evaluate_spy_connors(bars(recovery), is_long=True)
    assert exit_decision.action == "exit_next_open"


def test_mom_12_1_is_top_decile_with_deterministic_tie_breaking():
    symbols = [f"S{index:02d}" for index in range(20)]
    selection = rank_mom_12_1(
        formation_date=date(2026, 8, 31),
        intended_entry_date=date(2026, 9, 1),
        close_t={symbol: 10 for symbol in symbols},
        close_lag21={symbol: 10 + index for index, symbol in enumerate(symbols)},
        close_lag252={symbol: 10 for symbol in symbols},
        minimum_eligible=20,
    )
    assert selection.selected_count == 2
    assert selection.symbols == ("S19", "S18")
    assert selection.target_weight == pytest.approx(0.5)


def test_mom_12_1_fails_closed_on_thin_coverage():
    with pytest.raises(ValueError, match="data coverage halt"):
        rank_mom_12_1(
            formation_date=date(2026, 8, 31),
            intended_entry_date=date(2026, 9, 1),
            close_t={"SPY": 100},
            close_lag21={"SPY": 90},
            close_lag252={"SPY": 80},
        )


def test_recovered_mom_shadow_signal_requires_matching_state(tmp_path):
    report = tmp_path / "mom_12_1_shadow"
    signals = report / "signals"
    signals.mkdir(parents=True)
    (report / "state.json").write_text(
        '{"version":"mom_12_1_shadow_v1","universe_hash":"f7b50c2b0c0882df"}'
    )
    signal = signals / "2026-08-31.csv"
    signal.write_text(
        "strategy_version,formation_date,intended_entry_date,rank,symbol,score_12_1,formation_close,target_weight\n"
        "mom_12_1_shadow_v1,2026-08-31,2026-09-01,1,AAPL,1.2,200,0.5\n"
        "mom_12_1_shadow_v1,2026-08-31,2026-09-01,2,MSFT,1.1,300,0.5\n"
    )
    loaded = load_mom_shadow_signal(signal)
    assert loaded.symbols == ("AAPL", "MSFT")
    assert loaded.intended_execution_date.isoformat() == "2026-09-01"


def test_mom_refuses_a_late_same_day_entry_instead_of_backfilling():
    intended = date(2026, 9, 1)
    assert not _mom_signal_is_missed(
        intended,
        now=datetime(2026, 9, 1, 13, 29, tzinfo=UTC),  # 09:29 New York
    )
    assert _mom_signal_is_missed(
        intended,
        now=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),  # 09:30 New York
    )
