"""CrossSectionalReversalV2 driven through the REAL simulator. Same
percentile-injection fixture as the momentum family's tests, since both
families consume the identical `cross_sectional_momentum_percentile`
feature -- only the direction each extreme trades is inverted.
"""

from app.services.backtester import run_backtest
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS
from app.services.labs.intraday.families.v2.families import CrossSectionalReversalV2
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
    params = {**BASE_V2_PARAMETERS, **(param_overrides or {}), "strategy_architecture": "cross_sectional_reversal_v2"}
    strategy = CrossSectionalReversalV2(params, timeframe="30m")
    return run_backtest(candles, features, params, strategy, session_end_index=build_session_end_index(rows))


def test_buys_the_weakness_extreme_instead_of_the_strength_extreme():
    """Inverted from CrossSectionalMomentumV2: a LOW percentile (relative
    laggard) is bought here, betting on a short-term bounce."""
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.05}, {"direction": "long", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"], "did not buy the weakness extreme"
    assert result["trades"][0]["side"] == "long"


def test_shorts_the_strength_extreme_instead_of_the_weakness_extreme():
    """Inverted from CrossSectionalMomentumV2: a HIGH percentile (relative
    leader) is shorted here, betting on a short-term pullback."""
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.95}, {"direction": "short", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"], "did not short the strength extreme"
    assert result["trades"][0]["side"] == "short"


def test_does_not_fire_when_percentile_is_in_the_middle():
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(
        sessions, {10: 0.5}, {"direction": "long", "upper_percentile": 0.8, "lower_percentile": 0.2}
    )

    assert result["trades"] == []


def test_does_not_fire_when_the_percentile_is_unmeasured():
    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])

    result = run_with_percentile(sessions, {}, {"direction": "long"})

    assert result["trades"] == []


def test_registered_in_the_family_registry_as_active():
    assert "cross_sectional_reversal_v2" in FAMILY_REGISTRY
    assert FAMILY_REGISTRY["cross_sectional_reversal_v2"].status == "active"


def test_reversal_and_momentum_are_genuinely_mirror_images_not_duplicates():
    """Same percentile, same threshold, opposite family -- must produce
    opposite-direction trades, proving the two hypotheses are actually
    distinct rather than accidentally identical."""
    from app.services.labs.intraday.families.v2.families import CrossSectionalMomentumV2

    sessions = warmup(sessions=10)
    sessions.append([(100.0, 100.6, 99.6, 100.2, 1000) for _ in range(13)])
    candles, features = make_dataset(sessions)
    bars_per_session = len(sessions[0])
    for bar_index in range(bars_per_session):
        features[10 * bars_per_session + bar_index]["cross_sectional_momentum_percentile"] = 0.95
    rows = [{"candle": c, "feature": f} for c, f in zip(candles, features)]
    session_end_index = build_session_end_index(rows)

    momentum_params = {**BASE_V2_PARAMETERS, "direction": "both", "strategy_architecture": "cross_sectional_momentum_v2"}
    reversal_params = {**BASE_V2_PARAMETERS, "direction": "both", "strategy_architecture": "cross_sectional_reversal_v2"}

    momentum_result = run_backtest(
        candles, features, momentum_params, CrossSectionalMomentumV2(momentum_params, timeframe="30m"), session_end_index=session_end_index
    )
    reversal_result = run_backtest(
        candles, features, reversal_params, CrossSectionalReversalV2(reversal_params, timeframe="30m"), session_end_index=session_end_index
    )

    assert momentum_result["trades"][0]["side"] == "long"
    assert reversal_result["trades"][0]["side"] == "short"
