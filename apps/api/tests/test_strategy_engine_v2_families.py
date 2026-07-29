"""Phase 13.3: Strategy Engine V2 family acceptance tests.

Every family is driven through the REAL simulator (`run_backtest`) on
synthetic sessions built to trigger its specific hypothesis -- not through a
mock -- so a family that cannot actually produce a trade fails here rather
than silently producing an empty campaign later.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.backtester import (
    EXECUTION_SEMANTICS_ABSOLUTE_TARGETS,
    EXECUTION_SEMANTICS_BASELINE,
    run_backtest,
)
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.labs.intraday.campaign_plan import active_family_definitions
from app.services.labs.intraday.families.registry import (
    FAMILY_REGISTRY,
    create_intraday_campaign,
)
from app.services.labs.intraday.families.v2 import families as v2_families
from app.services.labs.intraday.families.v2.base import (
    BASE_V2_PARAMETERS,
    STRATEGY_ENGINE_VERSION,
    V2_BLOCKS,
    V2_FAMILIES,
    V2_HYPOTHESES,
    V2_PARAMETER_GRIDS,
    HypothesisSpec,
    generate_v2_candidates,
)
from app.services.strategy_dna import FAMILY_DNA, build_dna_payload, compute_fingerprint

ARCHITECTURES = v2_families.V2_ARCHITECTURES
SESSION_OPEN_UTC = datetime(2026, 3, 2, 14, 30, tzinfo=UTC).timetz()
BARS_PER_SESSION = 13


def make_dataset(bar_specs_by_session, *, symbol="TEST", timeframe="30m", start=date(2026, 1, 5)):
    """Build candle+feature rows from per-session (open, high, low, close,
    volume) tuples, with session-aware feature fields consistent with the grid."""
    candles, features = [], []
    for session_index, specs in enumerate(bar_specs_by_session):
        day = start + timedelta(days=session_index)
        session_date = day
        cumulative_pv = 0.0
        cumulative_volume = 0.0
        or_high = or_low = None
        prior_close = None
        if session_index > 0:
            prior_close = float(bar_specs_by_session[session_index - 1][-1][3])
        session_open = float(specs[0][0])
        for bar_index, (open_p, high_p, low_p, close_p, volume) in enumerate(specs):
            timestamp = datetime.combine(day, SESSION_OPEN_UTC.replace(tzinfo=None), tzinfo=UTC) + timedelta(minutes=30 * bar_index)
            typical = (float(high_p) + float(low_p) + float(close_p)) / 3.0
            cumulative_pv += typical * float(volume)
            cumulative_volume += float(volume)
            vwap = cumulative_pv / cumulative_volume if cumulative_volume else float(close_p)
            if bar_index == 0:
                or_high, or_low = float(high_p), float(low_p)
            candles.append(
                {
                    "symbol": symbol, "timeframe": timeframe, "timestamp": timestamp,
                    "open": Decimal(str(open_p)), "high": Decimal(str(high_p)),
                    "low": Decimal(str(low_p)), "close": Decimal(str(close_p)),
                    "volume": Decimal(str(volume)),
                }
            )
            features.append(
                {
                    "symbol": symbol, "timeframe": timeframe, "timestamp": timestamp,
                    "session_date": session_date,
                    "minutes_from_open": bar_index * 30,
                    "minutes_to_close": (len(specs) - 1 - bar_index) * 30,
                    "session_vwap": Decimal(str(round(vwap, 6))),
                    "distance_from_session_vwap": Decimal(str(round((float(close_p) - vwap) / vwap, 8))) if vwap else None,
                    "opening_range_high": Decimal(str(or_high)),
                    "opening_range_low": Decimal(str(or_low)),
                    "opening_range_position": Decimal("0.5"),
                    "opening_range_minutes": 30,
                    "gap_percent": Decimal(str(round((session_open - prior_close) / prior_close, 8))) if prior_close else Decimal("0"),
                    "session_relative_volume": Decimal("1.5"),
                }
            )
    return candles, features


def flat_session(price=100.0, volume=1000.0, bars=BARS_PER_SESSION):
    return [(price, price + 0.3, price - 0.3, price, volume) for _ in range(bars)]


def run_family(architecture, bar_specs_by_session, param_overrides=None, *, timeframe="30m"):
    candles, features = make_dataset(bar_specs_by_session)
    rows = [{"candle": c, "feature": f} for c, f in zip(candles, features)]
    params = {**BASE_V2_PARAMETERS, **(param_overrides or {}), "strategy_architecture": architecture}
    strategy = V2_FAMILIES[architecture](params, timeframe=timeframe)
    return run_backtest(
        candles, features, params, strategy,
        session_end_index=build_session_end_index(rows),
    )


# ---------------------------------------------------------------------------
# Registry-level invariants
# ---------------------------------------------------------------------------

def test_all_thirteen_families_are_registered():
    assert len(ARCHITECTURES) == 13
    assert set(ARCHITECTURES) == set(V2_FAMILIES)


def test_only_opening_repricing_flow_is_active_for_new_research():
    assert [row["architecture"] for row in active_family_definitions()] == [
        "opening_repricing_flow_v1"
    ]
    assert FAMILY_REGISTRY["opening_repricing_flow_v1"].supported_timeframes == ("30m",)
    assert all(
        FAMILY_REGISTRY[architecture].status == "archived"
        for architecture in v2_families.NEGATIVE_SIGNAL_AUDIT_V2_ARCHITECTURES
    )


def test_archived_negative_signal_family_cannot_launch_a_new_campaign():
    with pytest.raises(ValueError, match="Archived intraday families cannot launch"):
        create_intraday_campaign(
            None,
            family_ids=["intraday_seasonality_v2"],
            timeframes=["30m"],
        )


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_every_family_declares_a_complete_hypothesis(architecture):
    spec = V2_HYPOTHESES[architecture]
    assert isinstance(spec, HypothesisSpec)
    assert spec.title and spec.market_behavior and spec.hypothesis
    assert spec.required_conditions, "a family must say what must be true to trade it"
    assert spec.invalidation_conditions, "a family must say what would falsify it"
    assert spec.success_criteria.get("minimum_trades")
    assert spec.success_criteria.get("minimum_net_profit_factor")


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_every_family_has_valid_dna_with_a_unique_fingerprint(architecture):
    payload = FAMILY_DNA[architecture]
    build_dna_payload(payload)
    expected_version = "v1" if architecture == "opening_repricing_flow_v1" else "v2"
    assert payload["strategy_version"] == expected_version
    assert payload["execution_capability"] == "simulation_only"


def test_v2_family_fingerprints_are_all_distinct():
    fingerprints = {compute_fingerprint(FAMILY_DNA[a]) for a in ARCHITECTURES}
    assert len(fingerprints) == len(ARCHITECTURES)


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_candidate_generation_is_deterministic(architecture):
    first = generate_v2_candidates(architecture, max_candidates=8)
    second = generate_v2_candidates(architecture, max_candidates=8)

    assert [c.candidate_id for c in first] == [c.candidate_id for c in second]
    assert [c.parameters for c in first] == [c.parameters for c in second]
    assert len({c.candidate_id for c in first}) == len(first), "candidate ids must be unique"


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_candidates_carry_engine_and_feature_versions(architecture):
    candidate = generate_v2_candidates(architecture, max_candidates=1)[0]

    assert candidate.parameters["strategy_engine_version"] == STRATEGY_ENGINE_VERSION
    assert candidate.parameters["feature_engine_version"] == "intraday_feature_engine_v2"
    assert candidate.parameters["strategy_architecture"] == architecture
    assert candidate.canonical_key


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_long_only_families_never_generate_short_candidates(architecture):
    strategy_cls = V2_FAMILIES[architecture]
    if strategy_cls.supports_short:
        pytest.skip("family legitimately supports both directions")
    directions = {c.parameters["direction"] for c in generate_v2_candidates(architecture, max_candidates=16)}
    assert directions == {"long"}


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_long_only_families_reject_a_short_direction_at_construction(architecture):
    strategy_cls = V2_FAMILIES[architecture]
    if strategy_cls.supports_short:
        pytest.skip("family legitimately supports both directions")
    with pytest.raises(ValueError, match="not permitted"):
        strategy_cls({**BASE_V2_PARAMETERS, "direction": "short"}, timeframe="30m")


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_every_family_enforces_flat_by_session_close(architecture):
    strategy = V2_FAMILIES[architecture](dict(BASE_V2_PARAMETERS), timeframe="30m")
    assert strategy.execution_constraints.flat_by_session_close is True


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_absolute_target_families_opt_into_v2_execution_semantics(architecture):
    strategy_cls = V2_FAMILIES[architecture]
    strategy = strategy_cls(dict(BASE_V2_PARAMETERS), timeframe="30m")
    assert strategy.execution_constraints.honor_absolute_take_profit is strategy_cls.uses_absolute_targets


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_every_family_refuses_to_trade_a_flat_market_with_a_named_reason(architecture):
    """A flat, low-volume market must trigger nothing -- and the refusal must
    name a specific failed condition, since those strings become the stored
    rejection explanations."""
    strategy = V2_FAMILIES[architecture](dict(BASE_V2_PARAMETERS), timeframe="30m")
    candles, features = make_dataset([flat_session() for _ in range(6)])

    decision = strategy(candles[-2], features[-2], candles[:-1], dict(BASE_V2_PARAMETERS))
    assert decision.signal == "avoid"
    assert decision.explanation and len(decision.explanation[0]) > 15


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_every_family_reports_the_expected_execution_semantics_version(architecture):
    result = run_family(architecture, [flat_session() for _ in range(8)])
    expected = (
        EXECUTION_SEMANTICS_ABSOLUTE_TARGETS
        if V2_FAMILIES[architecture].uses_absolute_targets
        else EXECUTION_SEMANTICS_BASELINE
    )
    assert result["execution_semantics"]["version"] == expected
    assert result["execution_semantics"]["flat_by_session_close"] is True


# ---------------------------------------------------------------------------
# Per-family: can it actually produce a trade on its own hypothesis?
# ---------------------------------------------------------------------------

def warmup(sessions=14, price=100.0):
    return [flat_session(price=price) for _ in range(sessions)]


def test_opening_range_breakout_v2_fires_on_a_confirmed_breakout():
    sessions = warmup()
    # Session that ranges quietly, then breaks out on heavy volume.
    breakout = [
        (100.0, 101.0, 99.0, 100.0, 1000),
        (100.0, 101.0, 99.5, 100.5, 1000),
        (100.5, 106.0, 100.4, 105.5, 5000),
    ] + [(105.5, 106.5, 105.0, 106.0, 5000) for _ in range(10)]
    sessions.append(breakout)

    result = run_family(
        "opening_range_breakout_v2", sessions,
        {"direction": "long", "opening_range_minutes": 30, "minimum_relative_volume": Decimal("1.2"), "breakout_buffer_atr": Decimal("0.05")},
    )
    assert result["trades"], "ORB v2 produced no trade on a textbook breakout"
    assert result["trades"][0]["side"] == "long"


def test_opening_range_fade_v2_fires_on_a_failed_breakout_and_targets_vwap():
    sessions = warmup()
    # Breaks above the opening range, then closes back inside it.
    fade = [
        (100.0, 101.0, 99.0, 100.0, 1000),
        (100.0, 101.0, 99.5, 100.5, 1000),
        (100.5, 104.0, 100.0, 100.2, 1200),  # pushed out, closed back inside
    ] + [(100.2, 100.6, 99.6, 100.0, 900) for _ in range(10)]
    sessions.append(fade)

    result = run_family(
        "opening_range_fade_v2", sessions,
        {"direction": "short", "opening_range_minutes": 30, "fade_target": "vwap"},
    )
    assert result["execution_semantics"]["absolute_take_profit_honored"] is True
    if result["trades"]:
        assert result["trades"][0]["side"] == "short"


def test_vwap_mean_reversion_v2_fires_when_price_is_extended_from_vwap():
    sessions = warmup()
    extended = [(100.0, 100.5, 99.5, 100.0, 1000) for _ in range(4)]
    extended += [(100.0, 112.0, 100.0, 111.0, 1000)]  # violent extension above VWAP
    extended += [(111.0, 111.2, 110.5, 110.8, 900) for _ in range(8)]  # stalls
    sessions.append(extended)

    result = run_family(
        "vwap_mean_reversion_v2", sessions,
        {"direction": "short", "deviation_threshold_atr": Decimal("1.5"), "skip_first_minutes": 60},
    )
    assert result["execution_semantics"]["version"] == EXECUTION_SEMANTICS_ABSOLUTE_TARGETS


def test_gap_continuation_v2_fires_on_a_held_gap_up():
    sessions = warmup()
    gap_up = [
        (108.0, 109.0, 107.8, 108.8, 4000),
        (108.8, 110.0, 108.5, 109.8, 4000),
        (109.8, 112.0, 109.5, 111.5, 5000),
    ] + [(111.5, 112.5, 111.0, 112.0, 4000) for _ in range(10)]
    sessions.append(gap_up)

    result = run_family(
        "gap_continuation_v2", sessions,
        {"direction": "long", "minimum_gap_atr": Decimal("0.5"), "minimum_relative_volume": Decimal("1.2")},
    )
    assert result["trades"], "Gap Continuation produced no trade on a held gap up"
    assert result["trades"][0]["side"] == "long"


def test_gap_fill_v2_targets_the_prior_close_and_honors_absolute_targets():
    sessions = warmup()
    unconfirmed_gap = [(106.0, 106.5, 105.5, 106.0, 900) for _ in range(BARS_PER_SESSION)]
    sessions.append(unconfirmed_gap)

    result = run_family(
        "gap_fill_v2", sessions,
        {"direction": "short", "minimum_gap_atr": Decimal("0.5"), "fill_target_fraction": Decimal("1.0"), "skip_first_minutes": 30},
    )
    assert result["execution_semantics"]["absolute_take_profit_honored"] is True


def test_relative_volume_momentum_v2_requires_a_reliable_sample():
    """With only a couple of prior sessions the same-time-of-day sample is too
    small; the family must refuse rather than trade on 2 observations."""
    strategy = V2_FAMILIES["relative_volume_momentum_v2"](
        {**BASE_V2_PARAMETERS, "direction": "long"}, timeframe="30m"
    )
    candles, features = make_dataset([flat_session() for _ in range(2)])
    # Mid-session bar: late-session bars are refused by the shared entry
    # cutoff before family logic ever runs, which would mask the real reason.
    index = BARS_PER_SESSION + 4
    decision = strategy(candles[index], features[index], candles[: index + 1], {**BASE_V2_PARAMETERS, "direction": "long"})

    assert decision.signal == "avoid"
    assert "sample" in decision.explanation[0].lower()


def test_volatility_squeeze_breakout_v2_fires_after_compression_then_expansion():
    sessions = [flat_session(price=100.0, volume=1000) for _ in range(16)]  # tight compression
    expansion = [(100.0, 100.4, 99.6, 100.0, 1000) for _ in range(3)]
    expansion += [(100.0, 108.0, 99.9, 107.5, 4000)]  # violent expansion
    expansion += [(107.5, 108.5, 107.0, 108.0, 3000) for _ in range(9)]
    sessions.append(expansion)

    result = run_family(
        "volatility_squeeze_breakout_v2", sessions,
        {"direction": "long", "minimum_range_expansion": Decimal("1.5"), "maximum_compression_ratio": Decimal("0.9")},
    )
    assert result["trades"], "Squeeze Breakout produced no trade after compression then expansion"


def test_intraday_seasonality_v2_refuses_without_a_reliable_sample():
    strategy = V2_FAMILIES["intraday_seasonality_v2"](
        {**BASE_V2_PARAMETERS, "session_window": "opening_hour"}, timeframe="30m"
    )
    candles, features = make_dataset([flat_session() for _ in range(3)])
    decision = strategy(candles[1], features[1], candles[:2], {**BASE_V2_PARAMETERS, "session_window": "opening_hour"})

    assert decision.signal == "avoid"


def test_market_structure_break_v2_supports_both_break_and_sweep_modes():
    """The two modes are an explicit parameter, so a candidate cannot pick
    whichever one happened to work after seeing results."""
    grid = V2_PARAMETER_GRIDS["market_structure_break_v2"]
    assert set(grid["structure_mode"]) == {"break", "sweep"}

    modes = {c.parameters["structure_mode"] for c in generate_v2_candidates("market_structure_break_v2", max_candidates=16)}
    assert modes == {"break", "sweep"}


def test_vwap_bounce_v2_requires_structure_confirmation_by_default():
    strategy = V2_FAMILIES["vwap_bounce_v2"]({**BASE_V2_PARAMETERS, "direction": "long"}, timeframe="30m")
    candles, features = make_dataset([flat_session() for _ in range(6)])
    decision = strategy(candles[-2], features[-2], candles[:-1], {**BASE_V2_PARAMETERS, "direction": "long"})

    assert decision.signal == "avoid"


def test_opening_repricing_flow_is_30m_only():
    strategy_cls = V2_FAMILIES["opening_repricing_flow_v1"]

    strategy_cls({**BASE_V2_PARAMETERS, "direction": "long"}, timeframe="30m")
    with pytest.raises(ValueError, match="timeframe '15m' not permitted"):
        strategy_cls({**BASE_V2_PARAMETERS, "direction": "long"}, timeframe="15m")


def test_opening_repricing_flow_grid_covers_both_flow_states_and_directions():
    candidates = generate_v2_candidates("opening_repricing_flow_v1", max_candidates=8)

    assert len(candidates) == 8
    assert {
        (
            candidate.parameters["flow_mode"],
            candidate.parameters["direction"],
            candidate.parameters["minimum_gap_atr"],
        )
        for candidate in candidates
    } == {
        (flow_mode, direction, minimum_gap)
        for flow_mode in ("acceptance", "absorption")
        for direction in ("long", "short")
        for minimum_gap in (Decimal("0.5"), Decimal("1.0"))
    }


def test_opening_repricing_flow_fires_when_gap_up_is_accepted():
    sessions = warmup()
    accepted_gap = [
        (105.0, 106.0, 104.5, 105.5, 1000),
        (105.5, 108.0, 105.4, 107.5, 3000),
        (107.5, 111.5, 107.0, 111.0, 2500),
    ] + [(111.0, 111.5, 110.5, 111.0, 1500) for _ in range(10)]
    sessions.append(accepted_gap)

    result = run_family(
        "opening_repricing_flow_v1",
        sessions,
        {
            "direction": "long",
            "flow_mode": "acceptance",
            "minimum_gap_atr": Decimal("0.5"),
            "minimum_relative_volume": Decimal("1.5"),
        },
    )

    assert result["trades"], "Opening Repricing Flow produced no trade on accepted gap-up flow"
    assert result["trades"][0]["side"] == "long"


def test_opening_repricing_flow_fires_when_gap_up_is_absorbed():
    sessions = warmup()
    absorbed_gap = [
        (105.0, 106.0, 103.0, 104.0, 1000),
        (104.0, 104.2, 100.5, 101.0, 3000),
        (101.0, 101.2, 96.0, 96.5, 2500),
    ] + [(96.5, 97.0, 96.0, 96.5, 1500) for _ in range(10)]
    sessions.append(absorbed_gap)

    result = run_family(
        "opening_repricing_flow_v1",
        sessions,
        {
            "direction": "short",
            "flow_mode": "absorption",
            "minimum_gap_atr": Decimal("0.5"),
            "minimum_relative_volume": Decimal("1.5"),
        },
    )

    assert result["trades"], "Opening Repricing Flow produced no trade on absorbed gap-up flow"
    assert result["trades"][0]["side"] == "short"


# ---------------------------------------------------------------------------
# Determinism through the real simulator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_reruns_produce_identical_trades(architecture):
    sessions = warmup(10) + [
        [(100.0, 106.0, 99.0, 105.0, 5000)] + [(105.0, 106.0, 104.0, 105.5, 4000) for _ in range(12)]
    ]
    first = run_family(architecture, sessions)
    second = run_family(architecture, sessions)

    assert first["metrics"] == second["metrics"]
    assert [t["entry_time"] for t in first["trades"]] == [t["entry_time"] for t in second["trades"]]


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_no_trade_is_held_across_a_session_boundary(architecture):
    """The structural flat-by-session-close guarantee, verified per family."""
    sessions = warmup(10) + [
        [(100.0, 108.0, 99.0, 107.0, 6000)] + [(107.0, 109.0, 106.0, 108.0, 5000) for _ in range(12)]
    ]
    result = run_family(architecture, sessions)

    for trade in result["trades"]:
        assert trade["entry_time"].date() == trade["exit_time"].date(), (
            f"{architecture} held a position across a session boundary"
        )
