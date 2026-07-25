"""CrossSectionalMomentumV2 driven through the REAL simulator, with
`cross_sectional_momentum_percentile` injected onto specific feature rows --
the standard v2-family test harness has no concept of a cross-symbol
ranking, so this family needs its own fixture rather than reusing
`run_family` from test_strategy_engine_v2_families.py.
"""

from app.services.backtester import run_backtest
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS
from app.services.labs.intraday.families.v2.families import CrossSectionalMomentumV2
from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
from tests.test_strategy_engine_v2_families import make_dataset, warmup


def run_with_percentile(sessions, percentile_by_session_index, param_overrides=None):
    candles, features = make_dataset(sessions)
    bars_per_session = len(sessions[0])
    for session_index, percentile in percentile_by_session_index.items():
        for bar_index in range(bars_per_session):
            row_index = session_index * bars_per_session + bar_index
            features[row_index]["cross_sectional_momentum_percentile"] = percentile

    rows = [{"candle": c, "feature": f} for c, f in zip(candles, features)]
    params = {**BASE_V2_PARAMETERS, **(param_overrides or {}), "strategy_architecture": "cross_sectional_momentum_v2"}
    strategy = CrossSectionalMomentumV2(params, timeframe="30m")
    return run_backtest(candles, features, params, strategy, session_end_index=build_session_end_index(rows))


def test_fires_long_when_percentile_is_at_the_strength_extreme():
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.95}, {"direction": "long", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"], "did not fire on an extreme-strength percentile"
    assert result["trades"][0]["side"] == "long"


def test_fires_short_when_percentile_is_at_the_weakness_extreme():
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.05}, {"direction": "short", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"], "did not fire on an extreme-weakness percentile"
    assert result["trades"][0]["side"] == "short"


def test_does_not_fire_when_percentile_is_in_the_middle():
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.5}, {"direction": "long", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"] == []


def test_does_not_fire_when_the_percentile_is_unmeasured():
    """No injected percentile at all -- the feature key is simply absent,
    exactly like a real bar with fewer than 3 peers in its cross-section."""
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(sessions, {}, {"direction": "long"})

    assert result["trades"] == []


def test_direction_gating_still_applies_on_top_of_the_percentile_signal():
    """A long-only candidate must not take the short signal a low percentile
    would otherwise produce -- same base-class gating every other family
    relies on."""
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.05}, {"direction": "long", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"] == []


def test_registered_in_the_family_registry_as_active():
    assert "cross_sectional_momentum_v2" in FAMILY_REGISTRY
    assert FAMILY_REGISTRY["cross_sectional_momentum_v2"].status == "active"
