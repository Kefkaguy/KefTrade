"""Phase 12.3: shared acceptance harness for the 6 new Intraday Lab families.

Explicitly *not* a repeat of Opening-Range Breakout v1 / VWAP Reversion v1's
20-test infrastructure-validation depth -- that rigor was justified when the
underlying simulator extension itself was new and unproven. Per the Phase
12.3 instruction, "we are no longer validating infrastructure... we are
expanding the research search space." What still matters here, and is
tested for every family: it produces a real setup under a realistic
scenario, the structural session-close exit still works, reruns are
deterministic, and -- the one lesson worth re-testing every time, since it
silently broke the very first ORB pilot -- the walk-forward split doesn't
swallow every job once the dataset is a realistic size.

Each family reads different feature fields, so `make_dataset` below is a
"kitchen sink" builder that populates every field any family might read
(gap_percent, session_vwap, opening_range_high/low, session_relative_volume)
consistently per bar; each test only overrides the handful of values that
matter for that family's own entry condition.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.backtester import combine_candles_features, run_backtest
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.labs.intraday.families.ema_trend_continuation import DEFAULT_EMA_TREND_CONTINUATION_PARAMETERS, EmaTrendContinuationStrategy
from app.services.labs.intraday.families.gap_fill import DEFAULT_GAP_FILL_PARAMETERS, GapFillStrategy
from app.services.labs.intraday.families.intraday_trend_pullback import DEFAULT_INTRADAY_TREND_PULLBACK_PARAMETERS, IntradayTrendPullbackStrategy
from app.services.labs.intraday.families.opening_fade import DEFAULT_OPENING_FADE_PARAMETERS, OpeningFadeStrategy
from app.services.labs.intraday.families.session_momentum import DEFAULT_SESSION_MOMENTUM_PARAMETERS, SessionMomentumStrategy
from app.services.labs.intraday.families.vwap_trend_continuation import DEFAULT_VWAP_TREND_CONTINUATION_PARAMETERS, VwapTrendContinuationStrategy


def session_bar_specs(
    session_date: date,
    count: int,
    *,
    bar_minutes: int = 30,
    session_vwap: Decimal = Decimal("100"),
    opening_range_high: Decimal = Decimal("101"),
    opening_range_low: Decimal = Decimal("99"),
    opening_range_minutes: int = 30,
    gap_percent: Decimal | None = Decimal("0"),
    relative_volume: Decimal | None = Decimal("2.0"),
    close_overrides: dict[int, Decimal] | None = None,
) -> list[dict]:
    close_overrides = close_overrides or {}
    specs = []
    for index in range(count):
        specs.append(
            {
                "session_date": session_date,
                "minutes_from_open": index * bar_minutes,
                "minutes_to_close": (count - 1 - index) * bar_minutes,
                "session_vwap": session_vwap,
                "opening_range_high": opening_range_high,
                "opening_range_low": opening_range_low,
                "opening_range_minutes": opening_range_minutes,
                "gap_percent": gap_percent,
                "session_relative_volume": relative_volume,
                "close": close_overrides.get(index, Decimal("100")),
            }
        )
    return specs


def make_dataset(bar_specs: list[dict], *, timeframe: str = "30m", symbol: str = "TEST") -> tuple[list[dict], list[dict]]:
    bar_minutes = 30 if timeframe == "30m" else 15
    candles: list[dict] = []
    features: list[dict] = []
    cursor = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    for spec in bar_specs:
        timestamp = cursor
        cursor += timedelta(minutes=bar_minutes)
        close = spec.get("close", Decimal("100"))
        candles.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "open": close,
                "high": close + Decimal("0.25"),
                "low": close - Decimal("0.25"),
                "close": close,
                "volume": Decimal("1000"),
            }
        )
        feature = {key: value for key, value in spec.items() if key != "close"}
        vwap = spec.get("session_vwap")
        feature["distance_from_session_vwap"] = (close - vwap) / vwap if vwap else None
        feature.update({"symbol": symbol, "timeframe": timeframe, "timestamp": timestamp})
        features.append(feature)
    return candles, features


def sustain_close_from(specs: list[dict], start_index: int, price: Decimal) -> None:
    for index in range(start_index, len(specs)):
        specs[index]["close"] = price


def run_family(strategy_cls, bar_specs: list[dict], params: dict, *, timeframe: str = "30m"):
    candles, features = make_dataset(bar_specs, timeframe=timeframe)
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)
    strategy = strategy_cls(params, timeframe=timeframe)
    result = run_backtest(candles, features, params, strategy, session_end_index=session_end_index)
    return candles, features, result, strategy


# --- Family-specific scenario builders: (strategy_cls, params, bar_specs, timeframe) ---
# Every scenario places a warmup session first (55-58 bars, never triggers)
# so the >=50-bar warmup lands the loop inside a SECOND session where the
# trigger sits early -- the realistic shape production data has (bar 50 of
# a real multi-session dataset is already well into a later session), not
# an artifact of the test.


def _gap_fill_scenario():
    warmup = session_bar_specs(date(2026, 1, 2), 55, gap_percent=Decimal("0"))
    triggered = session_bar_specs(date(2026, 1, 3), 15, gap_percent=Decimal("-0.01"))  # 1% gap down -> long fill
    sustain_close_from(triggered, 2, Decimal("99"))
    specs = warmup + triggered
    params = {**DEFAULT_GAP_FILL_PARAMETERS, "direction": "long", "entry_window_minutes": 120}
    return GapFillStrategy, params, specs


def _session_momentum_scenario():
    warmup = session_bar_specs(date(2026, 1, 2), 55)
    triggered = session_bar_specs(date(2026, 1, 3), 15)
    sustain_close_from(triggered, 7, Decimal("105"))  # clear upward momentum vs bar 0-6 baseline of 100
    specs = warmup + triggered
    params = {**DEFAULT_SESSION_MOMENTUM_PARAMETERS, "direction": "long"}
    return SessionMomentumStrategy, params, specs


def _intraday_trend_pullback_scenario():
    warmup = session_bar_specs(date(2026, 1, 2), 55, session_vwap=Decimal("100"))
    triggered = session_bar_specs(date(2026, 1, 3), 15, session_vwap=Decimal("100"))
    # Establish an uptrend (well above vwap) for the whole session, then a
    # brief pullback at bar 10 (recent high at 108, close pulls back to 106.5
    # -> ~1.4% pullback, above the 0.2% default threshold) while staying above vwap.
    sustain_close_from(triggered, 0, Decimal("108"))
    triggered[10]["close"] = Decimal("106.5")
    specs = warmup + triggered
    params = {**DEFAULT_INTRADAY_TREND_PULLBACK_PARAMETERS, "direction": "long"}
    return IntradayTrendPullbackStrategy, params, specs


def _ema_trend_continuation_scenario():
    warmup = session_bar_specs(date(2026, 1, 2), 55)
    triggered = session_bar_specs(date(2026, 1, 3), 15)
    # A steadily rising price series makes the fast EMA > slow EMA with
    # price above the fast EMA -- the long entry condition.
    for index in range(len(triggered)):
        triggered[index]["close"] = Decimal("100") + Decimal(index)
    specs = warmup + triggered
    params = {**DEFAULT_EMA_TREND_CONTINUATION_PARAMETERS, "direction": "long", "ema_fast_period": 3, "ema_slow_period": 6}
    return EmaTrendContinuationStrategy, params, specs


def _opening_fade_scenario():
    warmup = session_bar_specs(date(2026, 1, 2), 55)
    triggered = session_bar_specs(date(2026, 1, 3), 15)
    sustain_close_from(triggered, 1, Decimal("97"))  # extended below low (99) -> long fade
    specs = warmup + triggered
    params = {**DEFAULT_OPENING_FADE_PARAMETERS, "direction": "long"}
    return OpeningFadeStrategy, params, specs


def _vwap_trend_continuation_scenario():
    warmup = session_bar_specs(date(2026, 1, 2), 55, session_vwap=Decimal("100"))
    triggered = session_bar_specs(date(2026, 1, 3), 15, session_vwap=Decimal("100"))
    # Momentum reference (4 bars back) must be lower than the trigger bar's
    # close, in the same direction as the vwap extension.
    for index in range(len(triggered)):
        triggered[index]["close"] = Decimal("100") + Decimal(index) * Decimal("0.5")
    specs = warmup + triggered
    params = {**DEFAULT_VWAP_TREND_CONTINUATION_PARAMETERS, "direction": "long"}
    return VwapTrendContinuationStrategy, params, specs


SCENARIOS = {
    "gap_fill": _gap_fill_scenario,
    "session_momentum": _session_momentum_scenario,
    "intraday_trend_pullback": _intraday_trend_pullback_scenario,
    "ema_trend_continuation": _ema_trend_continuation_scenario,
    "opening_fade": _opening_fade_scenario,
    "vwap_trend_continuation": _vwap_trend_continuation_scenario,
}


@pytest.mark.parametrize("family_name", SCENARIOS.keys())
def test_family_produces_at_least_one_setup(family_name) -> None:
    strategy_cls, params, specs = SCENARIOS[family_name]()
    _, _, result, _ = run_family(strategy_cls, specs, params)
    assert len(result["trades"]) >= 1, f"{family_name} produced no trades under its firing scenario"


@pytest.mark.parametrize("family_name", SCENARIOS.keys())
def test_family_forced_session_close_exit_works(family_name) -> None:
    strategy_cls, params, specs = SCENARIOS[family_name]()
    unreachable_params = {**params, "stop_multiple": Decimal("50"), "reward_risk_multiple": Decimal("50")}
    if "stop_atr_multiple" in params:  # opening_fade uses a different key name
        unreachable_params = {**params, "stop_atr_multiple": Decimal("50"), "reward_risk_multiple": Decimal("50")}
    candles, _, result, _ = run_family(strategy_cls, specs, unreachable_params)
    assert len(result["trades"]) >= 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[-1]["timestamp"]


@pytest.mark.parametrize("family_name", SCENARIOS.keys())
def test_family_deterministic_reruns_produce_identical_trades(family_name) -> None:
    strategy_cls, params, specs = SCENARIOS[family_name]()
    _, _, result_1, _ = run_family(strategy_cls, specs, params)
    _, _, result_2, _ = run_family(strategy_cls, specs, params)
    assert result_1["trades"] == result_2["trades"]
    assert result_1["metrics"] == result_2["metrics"]


@pytest.mark.parametrize("family_name", SCENARIOS.keys())
def test_family_realistic_scale_walk_forward_regression_guard(family_name) -> None:
    """The one property directly responsible for the ORB pilot's first
    (silently zero-trade) run: at >=80 rows, run_backtest's walk-forward
    split activates, and walk_forward_train_ratio must leave a validation
    window large enough for the >=50-bar warmup to reach live bars."""
    strategy_cls, base_params, _ = SCENARIOS[family_name]()
    sessions = [session_bar_specs(date(2026, 1, 2) + timedelta(days=day), 20) for day in range(20)]
    specs = [bar for session in sessions for bar in session]  # 400 bars

    trigger_index = 15 * 20 + 5
    if family_name == "ema_trend_continuation":
        for index in range(trigger_index, len(specs)):
            specs[index]["close"] = Decimal("100") + Decimal(index - trigger_index)
    elif family_name == "vwap_trend_continuation":
        for index in range(trigger_index, len(specs)):
            specs[index]["close"] = Decimal("100") + Decimal(index - trigger_index) * Decimal("0.5")
    elif family_name == "intraday_trend_pullback":
        sustain_close_from(specs, trigger_index, Decimal("108"))
        specs[trigger_index + 10]["close"] = Decimal("106.5")
    elif family_name == "gap_fill":
        specs[trigger_index]["gap_percent"] = Decimal("-0.01")
        sustain_close_from(specs, trigger_index + 2, Decimal("99"))
        base_params = {**base_params, "entry_window_minutes": 600}
    elif family_name == "opening_fade":
        sustain_close_from(specs, trigger_index + 1, Decimal("97"))
    else:  # session_momentum
        sustain_close_from(specs, trigger_index + 7, Decimal("105"))

    params = {**base_params, "direction": "long"}
    _, _, result, _ = run_family(strategy_cls, specs, params)

    assert result["metrics"]["walk_forward"]["enabled"] is True
    assert len(result["trades"]) >= 1, f"{family_name} produced no trades on the realistic-scale dataset"


def test_no_family_specific_branching_in_the_registry_dispatch() -> None:
    """The dispatch mechanism (registry dict lookup) must stay generic --
    confirmed by construction (FAMILY_REGISTRY is a plain dict), and
    guarded here against a future regression back to if/elif branching."""
    import inspect

    from app.services import strategy_discovery

    source = inspect.getsource(strategy_discovery.make_strategy_definition)
    forbidden = ["gap_fill_v1", "session_momentum_v1", "opening_fade_v1", "vwap_trend_continuation_v1", "ema_trend_continuation_v1", "intraday_trend_pullback_v1"]
    hits = [term for term in forbidden if term in source]
    assert hits == [], f"make_strategy_definition must not branch on specific family architecture strings; found {hits}"
